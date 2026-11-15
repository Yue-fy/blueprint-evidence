"""O1 cycle 12: short vertical dimension strings re-read turned (stoa/ocr/vfix.py)."""

from __future__ import annotations

import json

from PIL import Image

from stoa.ocr import vfix


def _it(text, box, score=0.9, rot=0):
    return {"text": text, "box": [float(v) for v in box], "score": score, "rot": rot}


def test_candidates_are_tall_upright_lines_no_turned_read_covers():
    items = [
        _it("O'm", [130, 970, 187, 1076], 0.488),  # '3,07' up the left margin, read upside down
        _it("KÜCHE", [522, 932, 688, 1010], 0.999),  # wide: not a candidate
        _it("HISTORIC AMERICAN", [2000, 100, 2040, 900], 0.95, rot=90),  # already read turned
        _it("HIS", [2002, 120, 2038, 300], 0.4),  # an upright fragment under the turned read: covered
        _it("1", [10, 10, 14, 30], 0.9),  # too narrow
    ]
    assert vfix.candidates(items) == [0]


def test_plausible_rejects_hatching_and_single_glyph_turns():
    assert vfix._plausible("3,07", "O'm", 0.488)
    assert vfix._plausible("21'-9\"", "21-,1", 0.866)
    assert vfix._plausible("Female WC", "Fel", 0.717)
    assert not vfix._plausible("=≠=", "三+二", 0.411)
    assert not vfix._plausible("0 一  一  0", "口", 0.356)
    assert not vfix._plausible("I", "1", 0.711)  # an upright '1' is naturally tall
    assert vfix._plausible("0", "口", 0.387)  # one glyph only where the upright read was noise


def test_strips_two_turns_per_candidate_inside_the_strip():
    img = Image.new("RGB", (400, 300), "white")
    items = [_it("x", [100, 50, 130, 120], 0.4), _it("y", [300, 100, 320, 160], 0.5)]
    out = vfix.strips(img, items, [0, 1])
    assert len(out) == 1
    s, slots = out[0]
    assert sorted((k, rot) for k, rot, *_ in slots) == [(0, 90), (0, 270), (1, 90), (1, 270)]
    for _, _, x0, y0, x1, y1 in slots:
        assert 0 <= x0 < x1 <= s.width and 0 <= y0 < y1 <= s.height
        assert (x1 - x0) > (y1 - y0)  # turned: the tall crop is now wide


def test_merge_takes_the_better_turn_and_strips_line_bits():
    items = [_it("O'm", [130, 970, 187, 1076], 0.488), _it("290", [2058, 575, 2104, 687], 0.867)]
    slots = [(0, 90, 40, 30, 200, 80), (0, 270, 40, 130, 200, 180), (1, 90, 40, 230, 200, 280),
             (1, 270, 40, 330, 200, 380)]
    reads = [{"text": "LO'E", "box": [50, 35, 150, 75], "score": 0.41},
             {"text": "3,07-", "box": [50, 135, 150, 175], "score": 0.97},
             {"text": "06Z", "box": [50, 235, 150, 275], "score": 0.5},
             {"text": "-290-", "box": [50, 335, 150, 375], "score": 0.997}]
    out, n = vfix.merge(items, [(slots, reads)])
    assert n == 2
    assert out[0]["text"] == "3,07" and out[0]["rot"] == 270 and out[0]["vfix"]["was"] == "O'm"
    assert out[1]["text"] == "290" and out[1]["score"] == 0.997
    assert out[0]["box"] == items[0]["box"]  # boxes stay in the original frame


def test_merge_keeps_an_upright_reading_that_is_as_good():
    items = [_it("4.50", [1045, 766, 1071, 824], 0.95)]
    slots = [(0, 90, 40, 30, 200, 80), (0, 270, 40, 130, 200, 180)]
    reads = [{"text": "4.50", "box": [50, 135, 150, 175], "score": 0.97}]
    out, n = vfix.merge(items, [(slots, reads)])
    assert n == 0 and out[0] is items[0]


def test_apply_reads_one_strip_and_caches_a_sidecar(tmp_path, monkeypatch):
    from stoa import ocr

    img = tmp_path / "plan.png"
    Image.new("RGB", (400, 300), "white").save(img)
    d = {"size": [400, 300], "items": [_it("O'm", [100, 50, 130, 120], 0.45)], "seconds": 1.0, "cpu_s": 2.0}
    key = tmp_path / "plan.abc.json"
    monkeypatch.setattr(ocr, "_key", lambda p: key)
    calls = []

    def fake_read(p):
        calls.append(p)
        s = Image.open(p)
        # put a good reading in the second slot (270): rows start at y = 30, each crop + 50 px gap
        h = (s.height - 30 - 30 - 50) / 2
        y = 30 + h + 50 + h / 2
        return {"items": [{"text": "3,07", "box": [45, y - 5, 120, y + 5], "score": 0.97}], "cpu_s": 0.5}

    monkeypatch.setattr(ocr, "read", fake_read)
    monkeypatch.setattr(vfix, "ON", True)
    out = vfix.apply(str(img), d)
    assert out["items"][0]["text"] == "3,07" and out["vfix"] == 1 and out["cpu_s"] == 2.5
    assert len(calls) == 1 and ".vstrip0" in calls[0]
    side = json.loads(key.with_suffix(".vfix.json").read_text())
    assert side["n_fixed"] == 1 and side["tag"] == vfix.TAG
    # second time: from the sidecar, no OCR read
    again = vfix.apply(str(img), d)
    assert again["items"][0]["text"] == "3,07" and len(calls) == 1
    assert d["items"][0]["text"] == "O'm"  # the reading itself is never changed


def test_apply_is_a_no_op_when_off_or_on_a_strip(monkeypatch):
    d = {"size": [10, 10], "items": [_it("O'm", [1, 1, 4, 9], 0.4)]}
    monkeypatch.setattr(vfix, "ON", False)
    assert vfix.apply("x.png", d) is d
    monkeypatch.setattr(vfix, "ON", True)
    assert vfix.apply("/c/plan.abc.vstrip0.png", d) is d


def _box_plan(tmp_path):
    from PIL import ImageDraw

    im = Image.new("RGB", (400, 400), "white")
    dr = ImageDraw.Draw(im)
    dr.rectangle([170, 170, 230, 230], outline="black", width=3)  # a 55 x 55 px region (a court marking, a fixture)
    dr.rectangle([20, 20, 380, 380], outline="black", width=4)
    p = tmp_path / "plan.png"
    im.save(p)
    return str(p)


def test_a_region_smaller_than_its_label_is_not_the_room(tmp_path, monkeypatch):
    """X1 sl-292: 'Sports Hall' (78 x 16 px label) flood-filled a 1184 px² court marking = 1.61 m² @0.5."""
    from stoa.solvers.raster import measure as M

    p = _box_plan(tmp_path)
    wide = [175, 176, 225, 224]  # a 50 x 48 px label block inside it: 1.5 x its area > the 3136 px² region
    monkeypatch.setattr(M, "LABEL_FIT", 0.0)
    assert M.room_area(p, wide, 20.0, **M.FILL)["m2"] == 7.84
    monkeypatch.setattr(M, "LABEL_FIT", 1.5)
    assert M.room_area(p, wide, 20.0, **M.FILL) is None
    assert M.room_area(p, [185, 195, 215, 205], 20.0, **M.FILL)["m2"] == 7.84  # a label that fits: kept


def test_chain_values_and_no_single_room_answer_for_a_sum():
    from stoa.solvers.raster import textqa as Q
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    assert Q._chain_value("3,695") == 3.695 and Q._chain_value("3695") == 3.695 and Q._chain_value("3,07") == 3.07
    items = [TextItem(text=t, box=b, tile=0) for t, b in [
        ("3,50", [835, 94, 949, 157]), ("3,00", [438, 95, 554, 156]), ("3,55", [1660, 91, 1782, 158]),
        ("350", [476, 1356, 585, 1416]), ("3,00", [873, 1358, 983, 1419]), ("3695", [107, 521, 196, 671]),
        ("3,07", [130, 970, 187, 1076]), ("KÜCHE", [522, 932, 688, 1010]), ("WOHNZIMMER", [701, 641, 993, 693])]]
    tl = TextLayer(path="x.png", size=(2162, 1496), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=0.0)
    a = Q._chain_room_answer("x.png", "Wie groß ist die Küche?", tl, "raster")
    assert a is not None and a.value == 10.745 and a.unit == "m2"
    assert Q._chain_room_answer("x.png", "What is the combined floor area of the kitchen and the living room?", tl,
                                "raster") is None


def test_thousands_point_dimension_strings_are_not_room_numbers():
    from stoa.solvers.raster import apartment as A
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    items = [TextItem(text=t, box=b, tile=0) for t, b in [
        ("7.200", [100, 50, 160, 66]), ("7.800", [300, 50, 360, 66]), ("4.125", [500, 51, 560, 67]),  # a chain
        ("1.105", [200, 400, 250, 416]), ("CLASSROOM", [180, 380, 270, 396])]]
    tl = TextLayer(path="x.png", size=(800, 800), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=0.0)
    ids = [r["id"] for r in A._rooms(tl)]
    assert ids == ["1.105"]


def test_a_thickness_of_metres_is_capped():
    from stoa.solvers.raster import textqa as Q

    assert Q._THICK_Q.search("How thick is the wall between the two halves of the building?")
    assert Q._THICK_Q.search("Wie dick ist die Außenwand?")
    assert 14.0 * Q._LEN_M["m"] > Q.THICK_MAX_M and 22.0 * Q._LEN_M["cm"] < Q.THICK_MAX_M
    assert 36 * Q._LEN_M["in"] < Q.THICK_MAX_M


def test_room_size_from_a_width_under_the_name_and_a_turned_depth():
    """D1 al0493 (HABS, 1/8" = 1'-0"): 'LIVING ROOM' / '15-6' under it / '16-4' turned above it = 253.17 sf; the outside
    chain's '13'-3"' beside the room is not its depth."""
    from stoa.solvers.raster import textqa as Q
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    items = [TextItem(text=t, box=b, tile=0) for t, b in [
        ("LIVING Room", [1863, 1561, 2057, 1596]), ("15-6", [1929, 1631, 1994, 1657]), ("16-4", [2014, 1484, 2047, 1554]),
        ("13'-3\"", [2149, 1612, 2182, 1681]), ("14'-0\"", [818, 407, 889, 437]), ("10'-9", [1706, 1839, 1786, 1870]),
        ("8'-7\"", [2008, 1839, 2078, 1868]), ("BED ROOM", [1858, 1263, 2037, 1298])]]
    tl = TextLayer(path="x.jpg", size=(3000, 2291), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=0.0)
    a, b, u, _, its = Q._hv_size(tl, items[0])
    assert (a, b, u) == (15.5, 16.3333, "ft") and its[1].text == "16-4"
    ans = Q._printed_size_answer("x.jpg", "How big is the living room?", tl, "raster")
    assert ans is not None and ans.value == 253.17 and ans.unit == "ft2"
    # a metric sheet (no proper ft-in strings): no loose '15-6' reading
    metric = TextLayer(path="x.jpg", size=(3000, 2291), items=items[:3], tiles=1, calls=0, tokens_in=0, tokens_out=0,
                       seconds=0.0)
    assert Q._hv_size(metric, items[0]) is None


def test_type_schedule_rows_and_a_digit_read_letter():
    """al0493 DOOR SCHEDULE: 'A-2-2-1"x9-9"×12"GLASS.' … 'H-2-6×7-0' and the J row read as '1-3'-0"×6'-6"'."""
    from stoa.solvers.raster import tags as TG
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    rows = ['A-2-2-1"x9-9"×12"GLASS.', 'B-3-2"×7-658×112"4TR.', "C-3'-0×7'-0\"×11/4\".", 'D-2-2-1"X8-9"GLASS.',
            'E-3-1"×7-6°×112"', 'F-3-2×7-6×114"', "G-2'-10\"×7'-0\"×13/6\"", "H-2-6×7-0", "1-3'-0\"×6'-6\""]
    items = [TextItem(text=t, box=[2414, 1032 + 32 * k, 2700, 1062 + 32 * k], tile=0) for k, t in enumerate(rows)]
    items.append(TextItem(text="1-10", box=[300, 300, 340, 330], tile=0))  # a dimension elsewhere: not a row
    tl = TextLayer(path="x.jpg", size=(3000, 2291), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=0.0)
    s = TG.schedule(tl)
    assert sorted(s) == list("ABCDEFGHJ") and "GLASS" in s["A"] and s["J"].startswith("J-3")
    few = TextLayer(path="x.jpg", size=(3000, 2291), items=items[:2], tiles=1, calls=0, tokens_in=0, tokens_out=0,
                    seconds=0.0)
    assert TG.schedule(few) == {}


def test_type_tag_question_forms():
    from stoa.solvers.raster import textqa as Q

    assert Q._TYPE_Q.search("How many type A doors are on the second floor plan?").group(1) == "A"
    assert Q._TYPED_KIND.search("How many glass doors are there?")
    assert Q._TAGGED_Q.search("How many doors are tagged on the second floor plan in total?")
    assert not Q._TYPE_Q.search("How many doors does a typical floor have?")
