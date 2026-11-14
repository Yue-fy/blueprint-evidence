"""Stacked fractions that lost their space in OCR / VLM text ('13'-11/2"' = 13'-1 1/2", D1 20:12)."""
import pytest

from stoa.solvers.raster import chains as C
from stoa.solvers.raster import textlayer as T


@pytest.mark.parametrize("s, inches", [
    ("11/2", 1.5), ("51/2", 5.5), ("33/4", 3.75), ("39/16", 3 + 9 / 16), ("113/16", 1 + 13 / 16),
    ("3/4", 0.75), ("15/16", 15 / 16), ("7", 7.0), ("11 1/2", 11.5), ("6.5", 6.5),
    ("24/2", 12.0),  # an even numerator is no reduced stacked fraction: the plain reading stays
])
def test_inch_value(s, inches):
    assert T.inch_value(s) == pytest.approx(inches)


def test_dimensions_read_stacked_fraction():
    tl = T.TextLayer(path="x.png", size=(100, 100), tiles=1,
                     items=[T.TextItem("13'-11/2\"", [0, 0, 10, 10]), T.TextItem("73'-9\"", [0, 20, 10, 30])])
    feet = sorted(d["feet"] for d in T.dimensions(tl))
    assert feet == [13.125, 73.75]


def test_chain_parse_stacked_fraction():
    assert C.parse("13'-11/2\"") == (13.125, "ftin")
    assert C.parse("4'-39/16\"") == (round(4 + 3.5625 / 12, 4), "ftin")
    assert C.parse("12'-6\"") == (12.5, "ftin")


@pytest.mark.parametrize("s, a, b", [
    ("15 1/2' × 39'", 15.5, 39.0), ("II'-3\" X IO'-0\"", 11.25, 10.0), ("17'-51/2\" x 12'-0\"", 17 + 5.5 / 12, 12.0),
    ("9'-4½\" x 10'", 9.375, 10.0), ("DRAWING RM. 18'-8\" × 26'-10\".", 18 + 8 / 12, 26 + 10 / 12),
])
def test_printed_size_with_fractions_and_lookalikes(s, a, b):
    from stoa.solvers.raster import textqa as Q

    got = Q._size_in(s)
    assert got is not None and got[2] == "ft"
    assert got[0] == pytest.approx(a) and got[1] == pytest.approx(b)


def test_rotated_label_takes_the_size_printed_beside_it(tmp_path):
    # HABS mt0047: 'KITCHEN' and "13'-6" X 21'-3"" both printed rotated, side by side
    from PIL import Image

    from stoa.solvers.raster import textqa as Q

    p = tmp_path / "rot.png"
    Image.new("L", (2000, 1500), 255).save(p)
    tl = T.TextLayer(path=str(p), size=(2000, 1500), tiles=1, items=[
        T.TextItem("KITCHEN", [780.0, 720.0, 804.0, 812.0]), T.TextItem("13'-6\" X 21'-3\"", [761.0, 719.0, 782.0, 826.0]),
        T.TextItem("PANTRY", [780.0, 916.0, 805.0, 1002.0]), T.TextItem("6'-11\" X 14'-11\"", [760.0, 905.0, 781.0, 1010.0])])
    a = Q._printed_size_answer(str(p), "What is the floor area of the kitchen?", tl, "raster")
    assert a is not None and a.value == pytest.approx(13.5 * 21.25, abs=0.01) and a.unit == "ft2"
    a = Q._printed_size_answer(str(p), "How big is the pantry?", tl, "raster")
    assert a is not None and a.value == pytest.approx((6 + 11 / 12) * (14 + 11 / 12), abs=0.01)


def test_sum_of_printed_sizes_for_a_plural_room_type(tmp_path):
    from PIL import Image

    from stoa.solvers.raster import textqa as Q

    p = tmp_path / "two.png"
    Image.new("L", (2000, 1500), 255).save(p)
    items = [T.TextItem("BEDROOM", [100.0, 100.0, 220.0, 125.0]), T.TextItem("10'-4\" x 12'-6\"", [95.0, 135.0, 230.0, 158.0]),
             T.TextItem("BEDROOM", [900.0, 100.0, 1020.0, 125.0]), T.TextItem("14'-5\" x 8'-11\"", [895.0, 135.0, 1030.0, 158.0])]
    tl = T.TextLayer(path=str(p), size=(2000, 1500), tiles=1, items=items)
    a = Q._rooms_sum_answer(str(p), "What is the combined floor area of the two bedrooms?", tl, [], "raster")
    assert a is not None and a.unit == "ft2"
    assert a.value == pytest.approx((10 + 4 / 12) * 12.5 + (14 + 5 / 12) * (8 + 11 / 12), abs=0.01)
    # the question names three, the sheet prints two: no guess
    assert Q._rooms_sum_answer(str(p), "What is the total area of the three bedrooms?", tl, [], "raster") is None
    assert Q._count_word("What is the total floor area of both bedrooms?") == 2


def test_find_label_letter_coded_and_abbreviated_rooms():
    from stoa.solvers.raster import measure as M

    tl = T.TextLayer(path="x.png", size=(2000, 1500), tiles=1, items=[
        T.TextItem("ROOM D", [989.0, 1165.0, 1051.0, 1186.0]), T.TextItem("ROOM E", [1200.0, 1165.0, 1262.0, 1186.0]),
        T.TextItem("DRAWING RM. 18'-8\" × 26'-10\".", [1350.0, 1908.0, 1774.0, 1938.0])])
    assert M.find_label(tl, "What is the area of Room D?", loose=True).text == "ROOM D"
    assert M.find_label(tl, "What is the floor area of the drawing room?", loose=True).text.startswith("DRAWING RM.")
    assert M._wkey("Head/Mtg") == "head mtg" and M._wkey("Bedroom 2") == "bedroom 2"
