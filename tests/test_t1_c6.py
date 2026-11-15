"""T1 cycle 6: H 23:44's two confident-wrong rows on Stoa's own sheets.

1. S-100F PDF 'What is the combined slab-on-grade area, in m²?' -> pdf_text 750 m² @0.55 = 300 + 150 + 300, the slab
   THICKNESSES of the three '<NNN> SLAB-ON-GRADE' callouts (gold 306.8). A value printed in none of the cited lines but
   equal to a sum of one number from each is rejected when every such sum adds numbers the asked kind can never be.
2. AFCEC PNG 'How many SF of dormitory space does the west wing have?' -> vlm_lora 6092 COUNT @0.75: 'how many SF' is
   an area question (stoa.router.intent._QTY_AFTER_COUNT), so the printed area readers answer (6,095 SF = gold).
"""
from stoa.contract import Answer, Evidence
from stoa.trust import support as S

Q_SLAB = "What is the combined slab-on-grade area, in m²?"
SLAB_TEXTS = ["300 SLAB-ON- / GRADE W/ D22 / @ 300 / TOP OF SLAB / EL. = 10000",
              "150 SLAB-ON-GRADE W/ / D13 @ 300 / TOP OF SLAB EL. = 10000",
              "300 SLAB-ON-GRADE W/ D16 / @ 300 TOP FACE. BOTTOM / FACE REINFORCING AS / INDICATED. / TOP OF SLAB EL. = 9400"]


def _ans(value, texts, unit="m2", conf=0.55):
    return Answer(value=value, unit=unit, confidence=conf, solver="pipeline/pdf_text",
                  evidence=[Evidence(file="s.pdf", page=0, bbox=[10 * i, 10, 10 * i + 5, 15], note=f"text: {t}")
                            for i, t in enumerate(texts)])


def test_sum_of_slab_thicknesses_is_not_an_area():
    v = S.check_citation(Q_SLAB, 750.0, SLAB_TEXTS, unit="m2")
    assert v is not None and not v.ok and v.role == "thickness"
    assert S.SUM_TAG in v.why and "300 + 150 + 300" in v.why


def test_sum_rejection_caps_then_withholds():
    a = S.citation_guard(Q_SLAB, _ans(750.0, SLAB_TEXTS))
    assert a.confidence <= S.REJECT_CONF  # -> E1's prefer_slab_area (conf < 0.45) takes over in Pipeline.solve
    out = S.final_guard(Q_SLAB, _ans(750.0, SLAB_TEXTS))
    assert out.value is None and out.missing[0].startswith("no printed area")


def test_sum_of_printed_areas_still_answers():
    texts = ["KITCHEN 12.5 m2", "LIVING 21.0 m2"]
    assert S.check_citation("What is the total area of the kitchen and the living room?", 33.5, texts, unit="m2") is None
    # unitless printed room areas: no role says 'never an area'
    assert S.check_citation("What is the total area of the two rooms?", 33.5, ["KITCHEN 12.5", "LIVING 21.0"],
                            unit="m2") is None


def test_sum_with_one_usable_reading_is_left_alone():
    # '12.5' plain + '21' plain: the sum has a usable reading even though the lines also print a ceiling height
    texts = ["KITCHEN 12.5 CLG HT 2700", "LIVING 21.0 CLG HT 2700"]
    assert S.check_citation("What is the total area of the two rooms?", 33.5, texts, unit="m2") is None


def test_sum_of_printed_counts_still_answers():
    assert S.check_citation("How many dorm rooms are there in total?", 144, ["48 DORMS", "48 DORMS", "48 DORMS"]) is None


def test_single_line_or_printed_value_is_not_a_sum_case():
    assert S.check_citation(Q_SLAB, 750.0, SLAB_TEXTS[:1], unit="m2") is None  # one line: nothing to add up
    v = S.check_citation(Q_SLAB, 300.0, SLAB_TEXTS, unit="m2")  # printed literally: the literal role check decides
    assert v is not None and not v.ok and S.SUM_TAG not in v.why


def test_how_many_sf_is_an_area_question():
    from stoa.router.intent import parse

    for q in ("How many SF of dormitory space does the west wing have?", "How many sf is the west wing?",
              "How many GSF is the building?", "How many S.F. is the lobby?", "How many sqm is the kitchen?"):
        p = parse(q)
        assert p.task == "area", q
    assert parse("How many SF of dormitory space does the west wing have?").unit == "ft2"
    assert parse("How many sqm is the kitchen?").unit == "m2"
    for q in ("How many of SF-1 tags are there?", "How many sofas are there?", "How many SFD units are shown?",
              "How many doors are there?"):
        assert parse(q).task == "count", q


# 3. EFA C2 scan 'According to the GIFA table, how big is the second floor?' -> area_tags 4055 m² @0.80 from the sheet's
#    'Overall GIFA: 4055m²' line (gold 2253 = 'Second: 2253m²'): a part question answered from a whole-building line.
def _marked(v, note, unit="m2", conf=0.8, solver="pipeline/area_tags"):
    return Answer(value=v, unit=unit, confidence=conf, solver=solver,
                  evidence=[Evidence(file="a.png", page=0, bbox=[1, 1, 5, 5], note=note)])


def test_part_question_from_whole_building_line_is_flagged():
    from stoa.trust import sanity as T

    q = "According to the GIFA table, how big is the second floor?"
    out = T.sanity_guard(q, _marked(4055.0, "text: Overall GIFA: 4055m²"))
    assert out.value == 4055.0 and out.confidence <= T.PARTS_CAP
    assert out.missing[0].startswith(T.OPPOSITE) and "second floor" in out.missing[0]
    assert S.support_state(q, out)["state"] == "unlocated"


def test_whole_line_controls_are_left_alone():
    from stoa.trust import sanity as T

    q = "According to the GIFA table, how big is the second floor?"
    a = _marked(2253.0, "printed: Overall GIFA Second: 2253m 2")  # the line names the floor
    assert T.whole_guard(q, a) is a
    a = _marked(4499.0, "TOTAL COMMON SPACE = 4,499 SF (printed area table)", unit="ft2")
    assert T.whole_guard("How large is the total common space on the third floor?", a) is a  # asks a total itself
    a = _marked(4055.0, "text: Overall GIFA: 4055m²")
    assert T.whole_guard("What is the gross internal area of the building?", a) is a  # no part named
    # the agent's notes carry its own sub-question ('What is the total area …') before the quoted printed text
    a = _marked(6285.0, "What is the total area in square feet of all dormitorie — text '6,285 SF'", unit="ft2",
                conf=0.4, solver="pipeline/agent")
    assert T.whole_guard("How many square feet of dormitory does the north wing have?", a) is a


# 4. A length 'supported' by ONE bare dimension token printed to the millimetre ('18.880' for 'How long is the main
#    hall?' on EFA scans): right 6 / 30 in the replay -> approximate (value and confidence unchanged).
def test_bare_mm_dimension_for_a_length_is_approximate():
    q = "How long is the main hall?"
    a = Answer(value=18.88, unit="m", confidence=0.75, solver="pipeline/raster",
               evidence=[Evidence(file="a.png", page=0, bbox=[1, 1, 5, 5], note="text '18.880'")])
    st = S.support_state(q, a)
    assert st["state"] == "approximate" and "18.880" in st["why"]
    a = Answer(value=12000.0, unit="mm", confidence=0.75, solver="pipeline/raster",  # a bare integer stays supported
               evidence=[Evidence(file="a.png", page=0, bbox=[1, 1, 5, 5], note="text '12000'")])
    assert S.support_state("How wide is the pump room part overall?", a)["state"] == "supported"


# 5. F8 00:13 / 00:31 (report ppmCheck, ported): a picture's area whose marked outline, measured at the reader's own
#    scale bar, is > 5 % from the value -> approximate (replay 800 files: 0 right / 12 wrong flagged).
def _pic_area(v, poly, note="Music 1 outline", trace=("[raster] scale bar: 1 m = 20.0 px",), assumptions=()):
    return Answer(value=v, unit="m2", confidence=0.3, solver="pipeline/raster", trace=list(trace),
                  assumptions=list(assumptions),
                  evidence=[Evidence(file="plan.png", bbox=[0, 0, 200, 200], bbox_space="image_px", note=note,
                                     polygon=poly)])


SQ = [[0, 0], [200, 0], [200, 200], [0, 200]]  # 40,000 px² at 20 px per m = 100 m²


def test_scale_bar_disagreement_is_approximate():
    st = S.support_state("How big is Music 1?", _pic_area(120.0, SQ))
    assert st["state"] == "approximate" and "scale bar" in st["why"] and "100.0 m²" in st["why"]


def test_scale_bar_controls():
    assert S.scale_bar_check(_pic_area(101.0, SQ), "area") is None  # agrees within 5 %
    assert S.scale_bar_check(_pic_area(500.0, SQ), "area") is None  # outlines cover < half: partial marks, not a check
    assert S.scale_bar_check(_pic_area(110.0, SQ, assumptions=("outline widened by the drawn line thickness",)),
                             "area") is None  # the reader's own stated widening
    assert S.scale_bar_check(_pic_area(120.0, SQ, trace=()), "area") is None  # no stated scale bar
    # a big picture read at a reduced size: the bar was measured on the reduced copy -> px per m x (4000 / 2000)
    a = _pic_area(25.0, SQ, trace=("large image (4000×3000 px) read at 2000×1500 px", "1 m = 20.0 px"))
    assert S.scale_bar_check(a, "area") is None  # 40,000 / 40² = 25 m²


# 6. Board NEXT (2): a count with NO marks that the sheet itself prints ('48 DORMS') marks the printed line, so the
#    answer is 'supported: the sheet prints 48 DORMS', not 'not counted' (printedcount runs before uncounted_guard).
def test_unmarked_count_printed_on_the_sheet_is_marked(monkeypatch):
    from stoa.solvers.raster.textlayer import TextItem
    from stoa.trust import printedcount as P
    from stoa.trust import sanity as T

    lines = [TextItem("ROOM COUNT", [10, 10, 90, 20]), TextItem("48 DORMS", [10, 30, 90, 40]),
             TextItem("TOTAL =", [10, 50, 50, 60]), TextItem("144 DORMS", [10, 70, 90, 80])]
    monkeypatch.setattr(P, "_lines", lambda path: (lines, "image_px"))
    q = "How many dormitory rooms are there on this level?"
    a = Answer(value=48, unit="count", confidence=0.7, solver="pipeline/vlm_lora", evidence=[])
    out = T.sanity_guard(q, a, "a.png")
    assert out.value == 48 and out.confidence == 0.7  # no 'uncounted' cap: the sheet prints this count
    assert not any(str(m).startswith(S.UNCOUNTED_MISSING) for m in out.missing)
    assert out.evidence[-1].bbox == [10, 30, 90, 40] and out.evidence[-1].bbox_space == "image_px"
    st = S.support_state(q, out)
    assert st["state"] == "supported" and "48 DORMS" in st["why"]
    # a different printed count: no box added, the guess stays a guess (uncounted) and says so
    out = T.sanity_guard(q, a.model_copy(update={"value": 12}), "a.png")
    assert out.confidence <= 0.35 and not out.evidence
    assert S.support_state(q, out)["state"] == "unlocated"
