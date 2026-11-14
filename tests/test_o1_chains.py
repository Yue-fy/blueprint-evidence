"""Dimension chains (stoa/solvers/raster/chains.py): a printed total its chain sums to = the overall dimension."""

from stoa.solvers.raster import chains as CH
from stoa.solvers.raster import textlayer as T


def _tl(items, size=(1200, 900)):
    return T.TextLayer(path="x.png", size=size, items=[T.TextItem(t, list(map(float, b))) for t, b in items])


def _row(y, segs, x0, k, h=16):
    """Horizontal chain strings centred on their segments: k px per unit of the segment values."""
    out, x = [], x0
    for text, v in segs:
        cx = x + v * k / 2
        out.append((text, [cx - 20, y - h / 2, cx + 20, y + h / 2]))
        x += v * k
    return out


def _col(x, segs, y0, k, w=16):
    out, y = [], y0
    for text, v in segs:
        cy = y + v * k / 2
        out.append((text, [x - w / 2, cy - 20, x + w / 2, cy + 20]))
        y += v * k
    return out


def _mm_plan():
    k = 0.0322  # px per mm (32.2 px per m)
    row = _row(225, [("2500", 2500), ("4000", 4000), ("1300", 1300), ("2700", 2700), ("3000", 3000)], 250, k)
    col = _col(157, [("3000", 3000), ("1500", 1500), ("4000", 4000), ("4015", 4015)], 300, k)
    items = row + col + [("-13 500", [440, 186, 500, 202]), ("12 515", [116, 480, 132, 540]),
                         ("BEDROOM", [400, 400, 470, 416]), ("1:50 Annotations", [500, 60, 600, 76])]
    return _tl(items)


def test_parse_dimension_strings():
    assert CH.parse("-13 500") == (13500.0, "thou")
    assert CH.parse("3000.") == (3000.0, "int")
    assert CH.parse("3,50") == (3.5, "dec")
    assert CH.parse("32'-0\"") == (32.0, "ftin")
    assert CH.parse("6'-7\"")[0] == round(6 + 7 / 12, 4)
    assert CH.parse("2'8\"")[0] == round(2 + 8 / 12, 4)
    assert CH.parse("+2,80") is None  # a level mark
    assert CH.parse("BEDROOM") is None


def test_overall_width_depth_and_area_mm():
    tl = _mm_plan()
    a = CH.overall_answer("x.png", "What is the overall width dimensioned on the floor plan?", tl)
    assert (a.value, a.unit) == (13.5, "m") and a.confidence == CH.VERIFIED_CONF
    a = CH.overall_answer("x.png", "What overall depth is dimensioned on the left side of that floor plan?", tl)
    assert (a.value, a.unit) == (12.515, "m")
    a = CH.overall_answer("x.png", "What area do the plan's two overall dimensions enclose?", tl)
    assert a.unit == "m2" and abs(a.value - 13.5 * 12.515) < 1e-3 and a.confidence == CH.AREA_CONF
    a = CH.overall_answer("x.png", "How long is the building?", tl)
    assert a.value == 13.5  # the longer of the two


def test_chain_scale_from_string_spacing():
    b = CH.chain_bar(_mm_plan())
    assert b is not None and abs(b.px_per_m - 32.2) < 0.2 and b.source == "chains"


def test_feet_inches_chain_and_footprint_in_square_metres():
    k = 37.5  # px per ft (1/4" = 1'-0" at 150 dpi)
    items = _row(237, [("12'", 12), ("8'", 8), ("12'", 12)], 400, k) + [("32'", [1003, 210, 1023, 226])]
    items += _col(2015, [("8'", 8), ("8'", 8)], 430, k) + [("16'", [2027, 722, 2043, 742])]
    tl = _tl(items, size=(2550, 1650))
    a = CH.overall_answer("x.png", "How long is the house (outside walls, without the porches)?", tl)
    assert (a.value, a.unit) == (32.0, "ft")
    a = CH.overall_answer("x.png", "How deep is the house from front to back?", tl)
    assert (a.value, a.unit) == (16.0, "ft")
    a = CH.overall_answer("x.png", "What is the footprint of the house without the porches, in square metres?", tl)
    assert a.unit == "m2" and abs(a.value - 47.57) < 0.01 and a.confidence == CH.FOOTPRINT_CONF


def test_no_answer_without_a_verified_total():
    # the total does not equal the chain's sum: no answer (the VLM path reads the sheet instead)
    items = _row(225, [("2500", 2500), ("4000", 4000), ("1300", 1300)], 250, 0.0322) + [("9 000", [380, 186, 440, 202])]
    assert CH.overall_answer("x.png", "What is the overall width of the building?", _tl(items)) is None


def test_question_kinds():
    assert CH.question_kind("How deep is the front porch?") is None
    assert CH.question_kind("What is the total length of water supply piping in the building?") is None
    assert CH.question_kind("What is the total floor area of the house?") is None  # not width x depth
    assert CH.question_kind("How tall is the house from the ground to the roof ridge?") is None
    assert CH.question_kind("Wie lang ist das Gebäude?") == ("length", None)
    assert CH.question_kind("Kuinka leveä talo on?") == ("width", "row")


def test_two_lengths_answer_an_area_question():
    from stoa.solvers.raster.textqa import two_lengths

    assert two_lengths("12.515m x 4.015m") == (12.515, 4.015, "m")
    assert two_lengths("3,50 x 3,00") == (3.5, 3.0, "m")
    assert two_lengths("350 x 300 cm") == (3.5, 3.0, "m")
    assert two_lengths("3500 x 2900") == (3.5, 2.9, "m")
    assert two_lengths("12'-6\" x 14'") == (12.5, 14.0, "ft")
    assert two_lengths("1.2 x 400") is None
    assert two_lengths("31.0 m²") is None


def test_ocr_fractions_and_lookalikes_in_feet_inches():
    assert CH.parse("16'-6·1/2\"")[0] == round(16 + 6.5 / 12, 4)  # HABS: a middle dot before the fraction
    assert CH.parse("9'-9-1/2\"")[0] == round(9 + 9.5 / 12, 4)
    assert CH.parse("II'-9\"")[0] == round(11 + 9 / 12, 4)  # OCR 'II' = 11 in a feet-inch string


def test_room_concepts_across_languages():
    from stoa.solvers.raster import measure as M

    tl = _tl([("WOHNZIMMER", [100, 100, 200, 120]), ("KÜCHE", [300, 100, 360, 120]), ("FLUR", [500, 100, 540, 120]),
              ("OH", [700, 100, 720, 120])])
    assert M.label_concept("WOHNZIMMER") == "living" and M.label_concept("OH") == "living"
    assert M.label_concept("KÜCHE") == "kitchen" and M.label_concept("FLUR") == "hall"
    assert M.question_concepts("What is the combined floor area of the kitchen and the living room?") == ["living", "kitchen"]
    assert M.find_label(tl, "How big is the hall?", loose=True).text == "FLUR"
    assert M.find_label(tl, "How big is the living room?", loose=True) is None  # two living-room labels: ambiguous


def test_landmark_room_is_not_the_asked_room():
    from stoa.solvers.raster import measure as M

    tl = _tl([("Salón", [100, 100, 160, 120]), ("Oficina", [400, 100, 470, 120])])
    assert M.find_label(tl, "What is the area of the small room next to the living room?", loose=True) is None
    assert M.find_label(tl, "How big is the living room?", loose=True).text == "Salón"
    assert M.find_label(tl, "How big is the Oficina next to the Salón?", loose=True).text == "Oficina"
    assert M.find_label(tl, "How big is the living room next to the office?", loose=True).text == "Salón"


def test_tag_count_over_the_whole_text_layer():
    from stoa.solvers.raster.textqa import _tag_count_answer

    items = [(f"PS-{n}", [10 * n, 10, 10 * n + 8, 18]) for n in (1, 3, 3, 4, 8, 8, 8)]
    items += [("PG", [10 * k, 50, 10 * k + 8, 58]) for k in range(5)] + [("PG = PRESSURE GAUGE", [0, 90, 120, 98])]
    items += [("FP-1", [0, 120, 20, 128]), ("FP-2", [30, 120, 50, 128])]
    tl = _tl(items)
    a = _tag_count_answer("x.png", "How many different pipe support types (PS-...) are called out?", tl, "raster")
    assert a.value == 4  # PS-1, PS-3, PS-4, PS-8
    assert _tag_count_answer("x.png", "How many pipe supports (PS) are there?", tl, "raster").value == 7  # codes repeat
    assert _tag_count_answer("x.png", "How many pressure gauges (PG) are tagged?", tl, "raster").value == 5  # legend out
    assert _tag_count_answer("x.png", "How many fuel pumps (FP) are shown?", tl, "raster").value == 2
    assert _tag_count_answer("x.png", "How many bedrooms (MH) are there?", _tl([("MH", [0, 0, 9, 9]), ("MH", [20, 0, 29, 9])]),
                             "raster") is None  # a room code: the room counters answer


def test_compass_axis_and_larger_rival():
    assert CH.question_kind("How long is the building east to west, overall?") == ("length", "row")
    assert CH.question_kind("How long is the building north-south?") == ("length", "col")
    # a verified vertical total (286') must not answer an east-west question; nothing verified across -> None
    k = 10.0
    items = _col(1000, [("177'-4\"", 177.333), ("108'-8\"", 108.667)], 100, k) + [("286'-0\"", [940, 1400, 960, 1500])]
    items += [("323'-4\"", [4369, 5725, 4637, 5820])]
    tl = _tl(items, size=(10200, 6600))
    assert CH.overall_answer("x.png", "How long is the building east to west, overall?", tl) is None
    assert CH.overall_answer("x.png", "How long is the building overall?", tl) is None  # 323'-4" is longer than 286'
