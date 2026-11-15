"""T1 c8: semantic checks on what the marks name (the asked rooms, a numbered series, one label = one object)."""
from __future__ import annotations

from stoa.contract import Answer, Evidence
from stoa.trust import calibrate as C
from stoa.trust import support as S


def _ans(value, ev, conf=0.75, unit="m2", solver="pipeline/plan_rooms", trace=None):
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=solver,
                  trace=list(trace or ["intent: area room [drawing] (lang en)"]))


def _outline(note, i):
    x = 100 * i
    return Evidence(file="efa.pdf", page=0, bbox=[x, 10, x + 90, 90], note=note,
                    polygon=[[x, 10], [x + 90, 10], [x + 90, 90], [x, 90]])


ART_Q = "What is the total floor area of the three art rooms (Art 1, Art 2 and Art 3)?"


def test_sl317_outlines_of_other_rooms_do_not_support_the_art_rooms():
    ev = [_outline(n, i) for i, n in enumerate(["MFL 1 55 m2: 54.83 m²", "MFL 2 55 m2: 54.79 m²", "MFL 3 55 m2: 54.84 m²",
                                                 "English 2 55 m2: 54.85 m²", "English 3 55 m2: 54.79 m²",
                                                 "English 1 55 m2: 54.85 m²"])]
    a = S.mark_support(ART_Q, _ans(328.95, ev))
    st = a.support or {}
    assert st.get("state") == "unlocated", st
    assert "MFL 1" in st["why"] and "art rooms" in st["why"]
    assert a.value == 328.95  # values unchanged
    # the calibration then keeps it a guess
    a.confidence = 0.75
    a2 = C.calibrate(a, "efa.pdf")
    assert a2.confidence <= C.UNLOCATED_MAX


def test_outlines_of_the_asked_rooms_stay_supported():
    ev = [_outline(f"Art {i} 55 m2: 54.8{i} m²", i) for i in (1, 2, 3)]
    st = S.support_state(ART_Q, _ans(164.5, ev)) or {}
    assert st.get("state") != "unlocated", st
    # a class of rooms: members carry their own names (English 1, MFL 2 are classrooms)
    ev = [_outline(n, i) for i, n in enumerate(["English 1: 54.85 m²", "MFL 2: 54.79 m²"])]
    assert S.marked_rooms_other("What is the total area of the classrooms?", ev) is None
    # unlabelled / own-word notes are unclear: no verdict
    assert S.marked_rooms_other(ART_Q, [_outline("traced outline", 0)]) is None
    assert S.marked_rooms_other("What is the size of the cross connection?", [_outline("text: 8\"", 0)]) is None


EN_Q = "What is the total floor area of the English classrooms?"


def _tags(nums, name="English", area=55):
    return [Evidence(file="efa.jpg", page=0, bbox=[100 * i, 10, 100 * i + 60, 40], note=f"{name} {n}: {area} m2")
            for i, n in enumerate(nums)]


def test_sl340_numbered_series_with_a_hole_is_doubted():
    a = S.mark_support(EN_Q, _ans(440.0, _tags([1, 2, 3, 4, 5, 6, 7, 9]), conf=0.77, solver="pipeline/area_tags"))
    st = a.support or {}
    assert st.get("state") == "approximate", st
    assert "English 8" in st["why"] and "unread" in st["why"]
    assert a.confidence <= S.DOUBT_CONF
    assert C.basis(st["why"]) == "doubt"


def test_complete_series_and_counts_are_not_gap_checked():
    st = S.support_state(EN_Q, _ans(495.0, _tags(range(1, 10)), solver="pipeline/area_tags")) or {}
    assert "unread" not in st.get("why", "")
    # the question names its own subset
    assert S.series_gap("Total area of English 1, English 2, English 3 and English 5?", _tags([1, 2, 3, 5])) is None
    # counts: tag series really skip numbers (M-101A prints PS-1 … PS-24 without PS-2, gold 23)
    ev = [Evidence(file="m.png", page=0, bbox=[100 * i, 10, 100 * i + 60, 40], note=f"PS-{n}")
          for i, n in enumerate([1] + list(range(3, 25)))]
    a = S.mark_support("How many different pipe support types (PS-...) are called out?",
                       _ans(23, ev, conf=0.8, unit="count", solver="pipeline/piping_img",
                            trace=["intent: count unknown [drawing] (lang en)"]))
    assert (a.support or {}).get("state") == "supported"


def _label(text, i=0):
    return Evidence(file="mfc.png", page=0, bbox=[3091 + 300 * i, 1929, 3317 + 300 * i, 1962], note=f"label '{text}'")


def test_sl353_note_fragment_label_is_not_one_room():
    a = S.mark_support("How many restrooms are there?",
                       _ans(1, [_label("RESTROOMS,")], conf=0.6, unit="count", solver="pipeline/label_count",
                            trace=["intent: count room [drawing] (lang en)"]))
    st = a.support or {}
    assert st.get("state") == "approximate", st
    assert "RESTROOMS," in st["why"]
    assert C.basis(st["why"]) == "contra"  # 0 right / 10 wrong on the replay: a guess, <= 0.39
    st = S.support_state("How many slow sand filters are there?",
                         _ans(1, [_label("SLOW SAND FILTERS")], unit="count", solver="pipeline/label_count")) or {}
    assert st.get("state") == "approximate" and "plural" in st["why"]


def test_singular_room_labels_stay_supported():
    st = S.support_state("How many plant rooms are there?",
                         _ans(3, [_label("Plant Room", i) for i in range(3)], unit="count",
                              solver="pipeline/label_count")) or {}
    assert st.get("state") == "supported", st
    # several plural labels ('Toilets' blocks) are not judged
    assert S.plural_labels("How many toilets are there?", [_label("Toilets", i) for i in range(3)]) is None


def test_bare_feet_inch_dimension_does_not_support_a_direction_length():
    ev = [Evidence(file="usda.pdf", page=0, bbox=[352.8, 548.7, 383.1, 555.8], note="text '40' - 0\"'")]
    a = _ans(40, ev, conf=0.75, unit="ft", solver="pipeline/raster",
             trace=["intent: length unknown [drawing] (lang en)", "[raster] answer '40'' from text lines [36] (literal match)"])
    st = S.support_state("How deep is the house?", a) or {}
    assert st.get("state") != "supported", st
    assert C.basis(st.get("why", "")) == "contra"
    # a labelled line stays supported
    ev = [Evidence(file="usda.pdf", page=0, bbox=[352.8, 548.7, 383.1, 555.8], note="text 'HOUSE DEPTH 24'-0\"'")]
    a = _ans(24, ev, conf=0.75, unit="ft", solver="pipeline/raster")
    st = S.support_state("How deep is the house?", a) or {}
    assert "has no label" not in st.get("why", "")


def test_text_search_marks_do_not_make_a_count_supported():
    ev = [Evidence(file="mfc.pdf", page=0, bbox=[10, 10 + 20 * i, 60, 25 + 20 * i], note=f"t1 row: 1.0{i}A") for i in range(6)]
    a = _ans(6, ev, conf=0.7, unit="count", solver="pipeline/pdf_text", trace=["intent: count door [drawing] (lang en)"])
    st = S.support_state("How many doors are listed in the door schedule?", a) or {}
    assert st.get("state") == "approximate", st
    assert C.basis(st["why"]) == "contra"
    a = _ans(6, ev, conf=0.7, unit="count", solver="pipeline/label_count", trace=["intent: count door [drawing] (lang en)"])
    assert (S.support_state("How many doors are listed in the door schedule?", a) or {}).get("state") == "supported"


def test_a_contra_check_never_raises_confidence():
    ev = [Evidence(file="mfc.pdf", page=0, bbox=[10, 10 + 20 * i, 60, 25 + 20 * i], note=f"t1 row: 1.0{i}A") for i in range(5)]
    a = S.mark_support("How many doors are listed in the door schedule?",
                       _ans(5, ev, conf=0.24, unit="count", solver="pipeline/pdf_text",
                            trace=["intent: count door [drawing] (lang en)"]))
    a = C.calibrate(a, "mfc.pdf")
    assert a.confidence <= 0.24


def test_a_count_answered_with_text_without_a_number_is_not_supported():
    from stoa.trust import sanity as SA
    q = "How many fire extinguisher cabinets (F.E.C.) are shown on the plan?"
    a = _ans("a / F.E.C.", [Evidence(file="a101.pdf", page=0, bbox=[10, 10, 60, 20], note="text: a / F.E.C.", exact=True)],
             conf=0.65, unit=None, solver="pipeline/pdf_text", trace=["intent: count unknown [drawing] (lang en)"])
    out = SA.text_shape_guard(q, a)
    assert out.confidence <= SA.SHAPE_CAP
    assert (S.support_state(q, out) or {}).get("state") == "unlocated"
    two = SA.text_shape_guard(q, _ans("TWO", [], conf=0.65, unit=None, solver="pipeline/pdf_text"))
    assert two.confidence == 0.65  # a number word may be a right count


def test_wrapped_bare_mm_dimension_is_not_supported():
    ev = [Evidence(file="efa.pdf", page=0, bbox=[10, 10, 60, 20], note="printed value '33,000' (the reading's number)",
                   exact=False)]
    a = _ans(33.0, ev, conf=0.3, unit="m", solver="pipeline/raster", trace=["intent: length room [drawing] (lang en)"])
    st = S.support_state("How wide is the sports hall?", a) or {}
    assert st.get("state") == "approximate" and "no label" in st["why"], st


def test_a_view_title_counted_as_a_room_is_doubted():
    ev = [Evidence(file="mfc.pdf", page=0, bbox=[173, 1951 + 140 * i, 183, 2021 + 140 * i], note=f"{i + 1}: {t} (printed label)")
          for i, t in enumerate(["Female Restrooms", "MODULE 05 - RESTROOMS - FLOOR PLAN", "Male Restrooms"])]
    a = S.mark_support("How many restrooms are there?",
                       _ans(3, ev, conf=0.65, unit="count", solver="pipeline/plan_rooms",
                            trace=["intent: count room [drawing] (lang en)"]))
    st = a.support or {}
    assert st.get("state") == "approximate" and "drawing's title" in st["why"], st
    assert a.confidence <= S.DOUBT_CONF and C.basis(st["why"]) == "doubt"
    # a door mark that names its view as the place stays
    ev = [Evidence(file="mfc.pdf", page=0, bbox=[10 + 50 * i, 10, 40 + 50 * i, 40],
                   note=f"door {i + 1} · MODULE 02-ADMINISTRATION - FLOOR PLAN · opening 0.92 m") for i in range(3)]
    assert S.title_mark(ev) is None


def test_uncounted_count_with_a_high_own_confidence_keeps_it_below_very_sure():
    from stoa.trust import sanity as SA
    a = _ans(1, [], conf=0.97, unit="count", solver="pipeline/vlm_lora",
             trace=['recognised: {"kind": "cubicasa_plan", "confidence": 0.99}', "intent: count bathroom [drawing] (lang en)"])
    a = SA.uncounted_guard("How many bathrooms are there?", a) if hasattr(SA, "uncounted_guard") else a
    a = S.mark_support("How many bathrooms are there?", a)
    assert (a.support or {}).get("state") == "unlocated"
    a = C.calibrate(a, "/tmp/x/plan.png")
    assert 0.85 <= a.confidence <= C.NOT_SUPPORTED_MAX, a.trace
    low = _ans(3, [], conf=0.7, unit="count", solver="pipeline/vlm_lora", trace=["intent: count door [drawing] (lang en)"])
    low = S.mark_support("How many doors are there?", SA.uncounted_guard("How many doors are there?", low))
    low = C.calibrate(low, "/tmp/x/sheet.png")
    assert low.confidence <= C.UNLOCATED_MAX
