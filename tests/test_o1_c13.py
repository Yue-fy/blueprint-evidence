"""O1 c13: which side of a room a printed dimension string measures (stoa/solvers/raster/sides.py)."""
from PIL import Image, ImageDraw

from stoa.solvers.raster import sides as SD
from stoa.solvers.raster import textlayer as T
from stoa.solvers.raster.textqa import _LEN_M


def _sheet(tmp_path):
    # a hall 200 x 400 px (10 x 20 m at 20 px/m), long side vertical; '10.150' printed horizontally above it
    im = Image.new("L", (400, 600), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([100, 100, 300, 500], outline=0, width=5)
    p = tmp_path / "hall.png"
    im.save(p)
    lab = T.TextItem("Main Hall", [160, 290, 240, 310])
    dim_h = T.TextItem("10.150", [180, 70, 220, 85])
    dim_v = T.TextItem("20.000", [70, 280, 85, 320])
    tl = T.TextLayer(path=str(p), size=(400, 600), items=[lab, dim_h, dim_v])
    return str(p), tl, dim_h, dim_v


def test_asked_side():
    assert SD.asked_side("How long is the main hall?") == "long"
    assert SD.asked_side("How wide is the sports hall?") == "wide"
    assert SD.asked_side("How long and how wide is the hall?") is None
    assert SD.asked_side("How deep is the hall?") is None


def test_text_axis():
    assert SD.text_axis(T.TextItem("10.150", [0, 0, 40, 15])) == "x"
    assert SD.text_axis(T.TextItem("10.150", [0, 0, 15, 40])) == "y"
    assert SD.text_axis(T.TextItem("22", [0, 0, 16, 14])) is None


def test_short_side_for_length_fires(tmp_path):
    p, tl, dim_h, _ = _sheet(tmp_path)
    note = SD.wrong_side("How long is the main hall?", 10.15, "m", [dim_h], tl, p, 20.0, _LEN_M)
    assert note and "short side" in note


def test_short_side_for_width_is_fine(tmp_path):
    p, tl, dim_h, _ = _sheet(tmp_path)
    assert SD.wrong_side("How wide is the main hall?", 10.15, "m", [dim_h], tl, p, 20.0, _LEN_M) is None


def test_long_side_for_length_is_fine(tmp_path):
    p, tl, _, dim_v = _sheet(tmp_path)
    assert SD.wrong_side("How long is the main hall?", 20.0, "m", [dim_v], tl, p, 20.0, _LEN_M) is None


def test_long_side_for_width_fires(tmp_path):
    p, tl, _, dim_v = _sheet(tmp_path)
    note = SD.wrong_side("How wide is the main hall?", 20.0, "m", [dim_v], tl, p, 20.0, _LEN_M)
    assert note and "long side" in note


def test_value_matching_the_asked_side_wins(tmp_path):
    # the outline says the horizontal string is the short side, but its value is the drawn long side: no claim
    p, tl, dim_h, _ = _sheet(tmp_path)
    assert SD.wrong_side("How long is the main hall?", 20.0, "m", [dim_h], tl, p, 20.0, _LEN_M) is None


def test_not_a_room_question(tmp_path):
    p, tl, dim_h, _ = _sheet(tmp_path)
    assert SD.wrong_side("How long is the wall of the main hall?", 10.15, "m", [dim_h], tl, p, 20.0, _LEN_M) is None
    assert SD.wrong_side("How long is the main hall from north to south?", 10.15, "m", [dim_h], tl, p, 20.0,
                         _LEN_M) is None


def test_off_switch(tmp_path, monkeypatch):
    p, tl, dim_h, _ = _sheet(tmp_path)
    monkeypatch.setattr(SD, "GUARD", False)
    assert SD.wrong_side("How long is the main hall?", 10.15, "m", [dim_h], tl, p, 20.0, _LEN_M) is None


def test_no_scale_no_claim(tmp_path):
    p, tl, dim_h, _ = _sheet(tmp_path)
    assert SD.wrong_side("How long is the main hall?", 10.15, "m", [dim_h], tl, p, None, _LEN_M) is None


def test_grid_line_band_does_not_turn_the_room(tmp_path):
    # a thin dash-dot grid line across the hall cuts the plain ink fill into a 10 x 5 m band (long side horizontal);
    # the walls-only fill still finds the 10 x 20 m hall, so the horizontal '10.150' is its SHORT side
    im = Image.new("L", (400, 600), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([100, 100, 300, 500], outline=0, width=5)
    for x in range(60, 340, 14):
        d.line([x, 250, x + 9, 250], fill=0, width=1)
    p = tmp_path / "grid.png"
    im.save(p)
    lab = T.TextItem("Main Hall", [160, 170, 240, 190])
    dim_h = T.TextItem("10.150", [180, 70, 220, 85])
    tl = T.TextLayer(path=str(p), size=(400, 600), items=[lab, dim_h])
    r = SD.outline_box(str(p), lab, tl, 20.0, skip=dim_h)
    assert r is not None and r["box"][3] - r["box"][1] > 350
    assert SD.wrong_side("How long is the main hall?", 10.15, "m", [dim_h], tl, str(p), 20.0, _LEN_M)


def test_fuzzy_label():
    tl = T.TextLayer(path="x", size=(10, 10), items=[T.TextItem("Sport Ha!!", [0, 0, 5, 1]),
                                                       T.TextItem("Entrance", [0, 2, 5, 3]),
                                                       T.TextItem("Hall", [0, 4, 5, 5])])
    assert SD.room_label(tl, "How wide is the sports hall?").text == "Sport Ha!!"
    assert SD.room_label(tl, "How wide is the kitchen?") is None


def test_check_estimates_the_asked_side(tmp_path):
    p, tl, dim_h, _ = _sheet(tmp_path)
    got = SD.check("How long is the main hall?", 10.15, "m", [dim_h], tl, p, 20.0, _LEN_M)
    assert got and abs(got["estimate"] - 20.3) < 0.05 * 20.3
    (xa, ya), (xb, yb) = got["line"]
    assert abs(xa - xb) < 1 and abs(ya - yb) > 350  # the measured line runs north-south across the hall


def test_printed_anywhere():
    from stoa.solvers.raster.textqa import _printed_anywhere

    class Bar:  # a scale bar's box: its labels never count as a printed value
        box = [0, 8, 10, 9]

    tl = T.TextLayer(path="x", size=(10, 10), items=[T.TextItem("33,000", [0, 0, 5, 1]),
                                                       T.TextItem("OPEN OFFICE", [0, 2, 5, 3]),
                                                       T.TextItem("11'-4 3/4\"", [0, 4, 5, 5]),
                                                       T.TextItem("20", [2, 8, 3, 9])])
    assert [i.text for i in _printed_anywhere("33.000", tl, [])] == ["33,000"]
    assert [i.text for i in _printed_anywhere("11'-4 3/4\"", tl, [])] == ["11'-4 3/4\""]
    assert _printed_anywhere("3.3", tl, []) == []  # '33' is not '33000'
    assert _printed_anywhere("20", tl, [Bar()]) == []  # a bar label
    assert _printed_anywhere("4", tl, []) == []  # one digit matches everywhere: no claim


def test_plural_area_words():
    from stoa.solvers.raster import chains, classes

    assert chains._AREA.search("Sum of all slab areas?")
    assert classes._MEASURE.search("Sum of all slab areas?")


def test_scale_is_no_size():
    import re
    from stoa.solvers.raster.textqa import _SCALE_Q, _SIZE

    assert _SIZE.search("How big is this room?") and not _SCALE_Q.search("How big is this room?")
    assert re.fullmatch(r"\s*(?:scale\s*)?1\s*[:/]\s*\d{1,5}\s*", "1:200", re.I)


def test_dims_label_keeps_room_numbers(monkeypatch):
    # 'How long is room 2.14 (Zimmer)?': the room-number label stays; a 'Zimmer' elsewhere must not replace it
    from stoa.solvers.raster import measure as M
    from stoa.solvers.raster import textqa as TQ

    tl = T.TextLayer(path="x.png", size=(400, 600), items=[T.TextItem("2.14", [180, 290, 220, 310]),
                                                           T.TextItem("Zimmer", [10, 10, 60, 25])])
    seen = []
    monkeypatch.setattr(M, "room_area", lambda path, box, *a, **k: seen.append(list(box)))
    monkeypatch.setattr(SD, "outline_box", lambda path, lab, *a, **k: seen.append(list(lab.box)))

    class Bar:
        px_per_m = 20.0

    TQ._room_dims_answer("x.png", "How long is room 2.14 (Zimmer)?", tl, [Bar()], "raster")
    assert seen and all(b == [180, 290, 220, 310] for b in seen)
    seen.clear()
    tl.items[0] = T.TextItem("Entrance", [180, 290, 220, 310])
    tl.items.append(T.TextItem("Sport Ha!!", [300, 300, 340, 320]))
    TQ._room_dims_answer("x.png", "How wide is the sports hall?", tl, [Bar()], "raster")
    assert seen and all(b == [300, 300, 340, 320] for b in seen)
