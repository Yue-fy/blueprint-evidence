"""O1 cycle 2: vertical scale bars, unquoted room names, solid-wall room fill, cm dimension chains (X1 wild exam)."""

import numpy as np
from PIL import Image, ImageDraw

from stoa.solvers.raster import measure as M
from stoa.solvers.raster import scalebar as SB
from stoa.solvers.raster import textlayer as T
from stoa.solvers.raster import textqa as Q


def _layer(path, size, items):
    return T.TextLayer(path=str(path), size=size, items=[T.TextItem(t, list(map(float, b))) for t, b in items])


def test_vertical_scale_bar(tmp_path):
    # a bar drawn top-down at x 100-108 from y 200 (0 m) to y 1000 (4 m), labels to its right: 200 px per m
    im = Image.new("L", (600, 1200), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([100, 200, 108, 1000], outline=0)
    for k in range(4):
        if k % 2:
            d.rectangle([100, 200 + 200 * k, 108, 400 + 200 * k], fill=0)
    p = tmp_path / "vbar.png"
    im.save(p)
    tl = _layer(p, im.size, [("0m", [115, 185, 140, 215]), ("2m", [115, 585, 140, 615]), ("4m", [115, 985, 140, 1015])])
    bars = SB._vertical_bars(str(p), tl, im, [])
    assert len(bars) == 1 and bars[0].vertical and bars[0].unit == "m"
    assert abs(bars[0].px_per_m - 200) < 3
    assert bars[0].box[1] <= 200 and bars[0].box[3] >= 1000  # mapped back to image (x, y)


def test_vertical_number_column_without_a_drawn_bar_is_not_a_bar(tmp_path):
    im = Image.new("L", (600, 1200), 255)
    p = tmp_path / "col.png"
    im.save(p)
    tl = _layer(p, im.size, [("0m", [115, 185, 140, 215]), ("2m", [115, 585, 140, 615]), ("4m", [115, 985, 140, 1015])])
    assert SB._vertical_bars(str(p), tl, im, []) == []


def test_find_label_loose_names():
    tl = _layer("x.png", (1000, 1000), [("Salón", [400, 400, 460, 420]), ("floor", [10, 900, 50, 920]),
                                         ("planner", [60, 900, 120, 920])])
    q = "What is the floor area of the Salón?"
    assert M.find_label(tl, q) is None  # strict: numbers / quoted names only
    assert M.find_label(tl, q, loose=True).text == "Salón"
    # two different names in the question: ambiguous
    tl.items.append(T.TextItem("Cocina", [600, 400, 660, 420]))
    assert M.find_label(tl, "Area of the Salón and the Cocina?", loose=True) is None


def test_solid_wall_fill_ignores_furniture(tmp_path):
    # a 4 x 3 m room (200 px/m) with black 0.2 m walls, a 1 m door gap, and grey desks with black outlines along
    # the walls: the ink fill stops at the desks, the solid-wall fill measures the room
    ppm = 200
    im = Image.new("L", (1400, 1200), 255)
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = 200, 200, 200 + 4 * ppm, 200 + 3 * ppm
    t = 40
    d.rectangle([x0 - t, y0 - t, x1 + t, y0 - 1], fill=0)
    d.rectangle([x0 - t, y1 + 1, x1 + t, y1 + t], fill=0)
    d.rectangle([x0 - t, y0, x0 - 1, y1], fill=0)
    d.rectangle([x1 + 1, y0, x1 + t, y1], fill=0)
    d.rectangle([x0 + 100, y1 + 1, x0 + 300, y1 + t], fill=255)  # 1 m door gap in the bottom wall
    for k in range(4):  # desks along the top wall, chairs in front
        d.rectangle([x0 + 10 + 190 * k, y0 + 2, x0 + 180 + 190 * k, y0 + 140], fill=215, outline=0, width=2)
        d.ellipse([x0 + 60 + 190 * k, y0 + 160, x0 + 120 + 190 * k, y0 + 220], fill=100, outline=0, width=2)
    p = tmp_path / "room.png"
    im.save(p)
    lab = [x0 + 380, y0 + 400, x0 + 440, y0 + 420]
    r = M.room_area_walls(str(p), lab, ppm)
    assert r is not None and abs(r["m2"] - 12.0) / 12.0 < 0.03
    got = M.room_area(str(p), lab, ppm, **M.FILL, solid=True)
    assert abs(got["m2"] - 12.0) / 12.0 < 0.03
    # another room's label inside the solid region: not one room -> keep the ink fill
    other = M.room_area(str(p), lab, ppm, **M.FILL, solid=True, others=[[x0 + 600, y0 + 400, x0 + 640, y0 + 420]])
    assert other is None or other["m2"] != got["m2"]


def test_cm_dimension_chain():
    tl = _layer("x.png", (1000, 1000), [("1104", [0, 0, 40, 10]), ("2.50", [0, 20, 40, 30]), ("3.00", [0, 40, 40, 50]),
                                         ("0.90", [0, 60, 40, 70]), ("scala 1:100", [0, 80, 90, 90])])
    cited = [tl.items[0]]
    assert Q._cm_chain(1104.0, "mm", "How wide is the hall?", cited, tl)
    assert Q._cm_chain(1104.0, "mm", "How many seats?", cited, tl) is None  # not a length question
    assert Q._cm_chain(2.5, "m", "How wide is the window?", [tl.items[1]], tl) is None
    tl.items.append(T.TextItem("all dimensions in mm", [0, 100, 90, 110]))
    assert Q._cm_chain(1104.0, "mm", "How wide is the hall?", cited, tl) is None


def test_user_scale_context():
    assert SB.user_bar() is None and SB.user_px_per_m() is None
    with SB.user_scale("33.2"):
        b = SB.user_bar()
        assert b is not None and abs(b.px_per_m - 33.2) < 1e-9 and b.evidence("x.png") == []
        assert "set by the user" in b.describe() and SB.scale_source(b) == "the scale set by the user"
        with SB.user_scale(None):  # an inner call without a scale keeps the outer one
            assert SB.user_px_per_m() == 33.2
    assert SB.user_px_per_m() is None
    with SB.user_scale("bogus"):
        assert SB.user_bar() is None


def test_raster_solver_takes_px_per_m():
    from stoa.solvers import get_solver

    assert get_solver("raster", px_per_m=40).px_per_m == 40.0
    assert get_solver("raster").px_per_m is None


def test_room_dims_rectangle():
    ppm = 100.0
    r = {"polygon": [[0, 0], [400, 0], [400, 300], [0, 300]]}
    d = M.room_dims(r, ppm)
    assert d["length_m"] == 4.0 and d["width_m"] == 3.0 and d["fill"] > 0.99
    assert abs(np.hypot(*np.subtract(*d["long_side"])) - 400) < 1 and abs(np.hypot(*np.subtract(*d["short_side"])) - 300) < 1
    # an L-shaped room: its bounding rectangle, flagged by fill < 0.9
    L = M.room_dims({"polygon": [[0, 0], [400, 0], [400, 300], [200, 300], [200, 150], [0, 150]]}, ppm)
    assert L["length_m"] == 4.0 and L["width_m"] == 3.0 and L["fill"] < 0.9
    assert M.room_dims({"polygon": []}, ppm) is None


def test_rectify_photo_of_a_sheet(tmp_path):
    from stoa.solvers.raster import rectify as R

    # a white sheet rotated ~5 deg on a dark table; a scan (sheet = whole frame) is left alone
    im = Image.new("RGB", (1200, 1000), (80, 80, 80))
    d = ImageDraw.Draw(im)
    d.polygon([(200, 150), (1000, 220), (950, 900), (150, 830)], fill=(245, 245, 245))
    d.rectangle([400, 400, 700, 600], outline=(0, 0, 0), width=6)
    p = tmp_path / "photo.jpg"
    im.save(p)
    got = R.rectified(str(p), cache_dir=str(tmp_path))
    assert got is not None
    flat, H = got
    w, h = Image.open(flat).size
    assert 700 < w < 900 and 600 < h < 800
    back = R.to_photo([[0, 0]], H)[0]
    assert abs(back[0] - 200) < 15 and abs(back[1] - 150) < 15
    scan = Image.new("RGB", (800, 600), (250, 250, 250))
    scan.save(tmp_path / "scan.png")
    assert R.rectified(str(tmp_path / "scan.png"), cache_dir=str(tmp_path)) is None


def test_level_as_scope_is_not_the_counted_object():
    from stoa.solvers.raster.classes import question_op

    assert question_op("How many meeting rooms are on this level?") == ("none", [])
    assert question_op("How many doors are on this level?") == ("single", ["door"])
    assert question_op("Wie viele Räume mit der Bezeichnung «Küche» gibt es in diesem Geschoss?") == ("single", ["kitchen"])
    assert question_op("How many levels does the building have?") == ("single", ["storey"])
    assert question_op("Kuinka monta kerrosta talossa on?") == ("single", ["storey"])


def test_printed_scale_with_image_dpi(tmp_path):
    """E1 15:13: a render's DPI tag + printed 1:N = an exact scale, but only on a standard sheet size."""
    p = tmp_path / "a3.png"
    Image.new("RGB", (2480, 1753), "white").save(p, dpi=(150, 150))  # 420 x 297 mm = A3 (the Swiss PNGs)
    assert SB.paper_of((2480, 1753), 150) == "A3"
    assert SB.paper_of((2480, 1753), 96) is None  # a default 96 dpi tag on a 150 dpi render: no paper size
    tl = _layer(p, (2480, 1753), [("M 1:100", [2000, 1600, 2100, 1620]), ("Wohnen", [500, 500, 560, 520])])
    b = SB.printed_bar(str(p), tl)
    assert b is not None and abs(b.px_per_m - 150 / 0.0254 / 100) < 0.01 and b.paper == "A3"
    assert "printed scale" in SB.scale_source(b)
    tl2 = _layer(p, (2480, 1753), [("1:100", [0, 0, 50, 10]), ("1:20", [0, 50, 50, 60])])
    assert SB.printed_bar(str(p), tl2) is None  # two ratios, neither labelled: which one is the plan's?
    tl3 = _layer(p, (2480, 1753), [("SCALE 1:100 @ A1", [0, 0, 50, 10])])
    b3 = SB.printed_bar(str(p), tl3)  # an A1 sheet's scale on an A3 copy: 1:200
    assert b3 is not None and abs(b3.ratio - 200) < 1


def test_opening_label_width_over_height():
    tl = _layer("x.png", (1000, 1000), [("0.90", [500, 100, 540, 120]), ("2.10", [500, 124, 540, 144]),
                                     ("2.50", [100, 600, 140, 620]), ("3.00", [100, 624, 140, 644])])
    door_h, door_w, win_h, win_w = tl.items
    got = Q._opening_pair(2.1, "How wide is the door into the office?", [door_w], tl)
    assert got is not None and got[0] == 0.9 and got[1] is door_h
    got = Q._opening_pair(0.9, "How tall is the door into the office?", [door_h], tl)
    assert got is not None and got[0] == 2.1
    got = Q._opening_pair(3.0, "How wide are the big windows?", [win_w], tl)
    assert got is not None and got[0] == 2.5
    assert Q._opening_pair(3.0, "How long is the hall?", [win_w], tl) is None
    one = _layer("x.png", (1000, 1000), [("0.80/2.10", [0, 0, 60, 20])])
    assert Q._opening_pair(2.1, "Wie breit ist die Tür?", one.items, one)[0] == 0.8


def test_level_difference_question_and_marks():
    assert Q._LEVEL_DIFF_Q.search("How much higher is the stage floor than the platea floor?")
    assert Q._LEVEL_DIFF_Q.search("Wie viel höher liegt die Bühne?")
    assert not Q._LEVEL_DIFF_Q.search("How high is the door?")
    assert Q._level_value("853.85") == 853.85 and Q._level_value("+2.85") == 2.85
    assert Q._level_value("-1,20") == -1.2 and Q._level_value("0.90") is None  # an opening size, not a level


def test_count_equal_to_its_cited_labels_is_grounded():
    items = [T.TextItem(t, [0, 0, 1, 1]) for t in ("W.C.", "W.C.", "W.C:")]
    assert Q._label_count(3, "How many W.C.s are on this floor?", items)
    assert Q._label_count(2, "How many W.C.s are on this floor?", items) is None  # not the cited labels
    assert Q._label_count(3, "What is the area of the W.C.?", items) is None
    mixed = [T.TextItem(t, [0, 0, 1, 1]) for t in ("W.C.", "BAGNO")]
    assert Q._label_count(2, "How many toilets?", mixed) is None


def test_room_sized_by_margin_dimension_chains():
    W, H = 2000, 1500
    tl = _layer("x.png", (W, H), [("3,50", [835, 94, 949, 157]), ("3,55", [1660, 91, 1782, 158]),
                                  ("3695", [107, 521, 196, 671]), ("290", [1900, 575, 1950, 687]),
                                  ("WOHNZIMMER", [701, 641, 993, 693]), ("3,00", [873, 1358, 983, 1419]),
                                  ("FLUR", [875, 949, 996, 1008])])
    a = Q._chain_room_answer("x.png", "How big is the living room (Wohnzimmer)?", tl, "raster")
    assert a is not None and abs(a.value - 3.5 * 3.695) < 1e-3 and a.unit == "m2"  # left chain, not the right one
    b = Q._chain_room_answer("x.png", "Wie breit ist der Flur?", tl, "raster")
    assert b is not None and b.value == 3.0
    assert Q._chain_value("1934") is None and Q._chain_value("350") == 3.5 and Q._chain_value("3695") == 3.695
