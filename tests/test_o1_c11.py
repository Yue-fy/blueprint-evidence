"""O1 c11: RCP ceiling-height tags are no widths, labels outside their room (leader arrows), scans of large CAD sheets
bridge door gaps, 'how many linear feet' is a length, cached_layer never starts an OCR read."""
import numpy as np
from PIL import Image, ImageDraw

from stoa.solvers.raster import measure as M
from stoa.solvers.raster import textlayer as T
from stoa.solvers.raster import textqa as Q
from stoa.solvers.raster import views as VW
from stoa.solvers.raster.classes import question_op


def _it(t, x0, y0, x1, y1):
    return T.TextItem(t, [float(x0), float(y0), float(x1), float(y1)])


def _module_sheet():
    """AFCEE A-101 layout: floor plan view on the left, its RCP twin on the right, titles below each view."""
    items = [
        _it("Break Room", 662, 3258, 780, 3283), _it("2.02", 690, 3283, 752, 3310),  # floor plan tag
        _it("Break Room", 2392, 3172, 2512, 3199), _it("9'-0\"", 2250, 3297, 2315, 3326),  # RCP tag + ceiling height
        _it("12'-6\"", 900, 3150, 970, 3175),  # a dimension in the floor plan
        _it("MODULE 02 - ADMINISTRATION - RCP", 2279, 3532, 2831, 3574),
        _it("MODULE 02-ADMINISTRATION- FLOOR PLAN", 891, 3551, 1553, 3594),
        _it("1/8\" = 1'-0\"", 899, 3594, 1055, 3633),
        _it("SCALE: 1/8\"= 1'", 5495, 3112, 5677, 3148),
        _it("501 North Broadway, St. Louis", 5366, 500, 5900, 535),
    ] + [_it(f"{k}.0{k}", 300 + 90 * k, 1000, 360 + 90 * k, 1027) for k in range(1, 8)]
    return T.TextLayer(path="x.png", size=(6000, 3882), items=items)


def test_rcp_ceiling_height_is_no_width():
    tl = _module_sheet()
    tits = VW.titles(tl)
    assert {k for k, _ in tits} == {"rcp", "floor"}
    tag = tl.items[3]
    assert VW.view_of(tag, tits) == "rcp" and VW.view_of(tl.items[1], tits) == "floor"
    assert VW.view_of(tl.items[9], tits) is None  # title block: beside every title
    q = "How wide is the opening into the Break Room 2.02 from the corridor?"
    assert "reflected ceiling plan" in VW.rcp_cited(q, [tl.items[8], tag], tl)  # the scale note is not a value
    assert VW.rcp_cited("How high is the ceiling of the Break Room?", [tag], tl) is None
    assert VW.rcp_cited("How tall is the Break Room?", [tag], tl) is None
    assert VW.rcp_cited(q, [tl.items[4]], tl) is None  # a floor-plan dimension stays


def test_rcp_guard_needs_a_floor_plan_title_and_skips_notes():
    tl = _module_sheet()
    no_floor = T.TextLayer(path="x.png", size=tl.size, items=[i for i in tl.items if "FLOOR PLAN" not in i.text])
    assert VW.rcp_cited("How wide is the Break Room?", [tl.items[3]], no_floor) is None
    note = _it("SEE RCP FOR CEILING HEIGHTS", 2279, 3600, 2831, 3642)
    assert all(t is not note for _, t in VW.titles(T.TextLayer(path="x.png", size=tl.size, items=tl.items + [note])))


def _leader_plan(tmp_path, leader=True, gray=120):
    """A 10 x 13 ft room (walls 6 px) with its tag outside on the left and a leader arrow into it."""
    W, H = 1400, 1000
    im = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = 500, 200, 720, 486  # inner faces
    for box in ([x0 - 6, y0 - 6, x1 + 6, y0], [x0 - 6, y1, x1 + 6, y1 + 6], [x0 - 6, y0 - 6, x0, y1 + 6],
                [x1, y0 - 6, x1 + 6, y1 + 6]):
        d.rectangle(box, fill=40)
    if leader:
        d.line([(462, 340), (600, 340)], fill=gray, width=1)
        d.polygon([(600, 333), (620, 340), (600, 347)], fill=gray)
    p = tmp_path / "leader.png"
    im.save(p)
    items = [_it("Break Room", 330, 315, 448, 340), _it("2.02", 358, 340, 420, 367), _it("153 SF", 356, 367, 430, 392),
             _it("Office", 900, 300, 980, 325), _it("2.03", 910, 325, 970, 352)]
    return str(p), T.TextLayer(path=str(p), size=(W, H), items=items)


def test_label_group_and_leader_tip(tmp_path):
    p, tl = _leader_plan(tmp_path)
    lab = tl.items[1]
    G = M.label_group(tl, lab)
    assert G[1] <= 315 and G[3] >= 392  # name + number + printed area
    tip = M.leader_tip(p, tl, lab, 72.0)
    assert tip is not None and abs(tip[0] - 620) <= 6 and abs(tip[1] - 340) <= 4
    p2, tl2 = _leader_plan(tmp_path, leader=False)
    assert M.leader_tip(p2, tl2, tl2.items[1], 72.0) is None


def test_room_area_follows_a_light_leader_when_the_tag_is_outside(tmp_path, monkeypatch):
    p, tl = _leader_plan(tmp_path, gray=200)  # lighter than the ink threshold (190), like the scanned PDF's leader
    lab = tl.items[1]
    others = [i.box for i in tl.items if i is not lab and M._ROOM_ID.fullmatch(i.text)]
    r0 = M.room_area(p, lab.box, 72.0, **M.FILL, others=others)
    r = M.room_area(p, lab.box, 72.0, **M.FILL, others=others, layer=tl)
    assert r0 is None  # the fill from the tag runs to the sheet border: no room
    assert r is not None and abs(r["m2"] - 220 * 286 / 72.0 ** 2) / (220 * 286 / 72.0 ** 2) < 0.04
    assert "leader line" in r["notes"][0]
    monkeypatch.setattr(M, "LEADER", False)
    r_off = M.room_area(p, lab.box, 72.0, **M.FILL, others=others, layer=tl)
    assert r_off is None or "leader line" not in r_off["notes"][0]


def test_linear_feet_of_wall_base_is_a_length_not_a_door_count():
    q = ("How many linear feet of wall base do we need for the Break Room (2.02)? No base across the open entrance "
         "from the corridor or across the closet doorway, and leave the closet itself out.")
    assert question_op(q) == ("none", [])
    assert Q._SKIRT.search(q)
    assert question_op("How many doors are there?") == ("single", ["door"])


def test_room_dims_never_answer_an_opening(tmp_path):
    p, tl = _leader_plan(tmp_path)

    class Bar:
        px_per_m = 72.0

    assert Q._room_dims_answer(p, "How wide is the opening into the Break Room 2.02 from the corridor?", tl, [Bar()],
                               "raster") is None
    assert Q._PART_Q.search("How wide is the entrance to the kitchen?")
    assert not Q._PART_Q.search("How long is the Entrance Hall?")


def test_imperial_sheet_answers_in_feet():
    class PB:
        unit, unit_text = "m", "1/8\" = 1'"

    class MB:
        unit, unit_text = "m", "1:100"

    assert Q._imperial_bar(PB()) and not Q._imperial_bar(MB())
    assert Q._METRIC_Q.search("perimeter in metres?") and not Q._METRIC_Q.search("What is the perimeter of the room?")


def test_cached_layer_never_starts_an_ocr_read(tmp_path, monkeypatch):
    from stoa import ocr
    from stoa.solvers.raster import sheet

    img = tmp_path / "scan.png"
    Image.new("L", (3000, 2000), 200).save(img)
    p = str(img)
    vlm = T.TextLayer(path=p, size=(3000, 2000), items=[_it("Kitchen", 10, 10, 60, 30)])

    class Key:
        def exists(self):
            return True

    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")
    monkeypatch.setattr(T, "_disk_key", lambda *a, **k: Key())
    monkeypatch.setattr(T, "ocr_suits", lambda path: True)
    monkeypatch.setattr(ocr, "cached", lambda path: False)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(T, "_vlm_layer", lambda *a, **k: vlm)

    def no_read(path):
        raise AssertionError("cached_layer started an OCR read")

    monkeypatch.setattr(ocr, "text_layer", no_read)
    T._mem.clear()
    assert sheet.cached_layer(p) is vlm
    T._mem.clear()


def test_level_annotations_answer_no_length():
    it = lambda t: T.TextItem(t, [0, 0, 10, 10])  # noqa: E731
    assert Q._level_cited(3986.0, [it("EACH END. IE =3986.00")])  # D1 22:24: an invert elevation
    assert Q._level_cited(87.5, [it("BRH 87.50")])  # X1 22:22: a sill height (Brüstungshöhe)
    assert not Q._level_cited(62, [it("FF-62")])  # DfE sheet name 'FF-62' = first floor, not a level
    assert not Q._level_cited(12.5, [it("12.50")])
    assert Q._SIZE.search("How long is the perimeter roadway centreline?")
    assert not Q._LEVEL_ASKED.search("How long is the perimeter roadway centreline?")
    for q in ("Wie hoch ist die Brüstung der Fenster?", "What is the invert elevation at MH1?", "How high is the sill?"):
        assert Q._LEVEL_ASKED.search(q), q


def test_inch_numbers_are_numbers():
    assert Q._inch_number("1 1/2", "inch") == 1.5
    assert Q._inch_number('1-1/2"', None) == 1.5
    assert Q._inch_number("1½ in", None) == 1.5
    assert Q._inch_number("6 inches", "inches") == 6.0
    assert Q._inch_number('3/4"', None) == 0.75
    assert Q._inch_number("1 1/2", None) is None  # no inch mark, no inch unit: may be a feet-inch string
    assert Q._inch_number("24", "inches") is None  # plain numbers take the normal path


def test_a_dashed_grid_line_past_the_tag_is_no_leader(tmp_path):
    """EFA 'Dining' render (sc-185): a dashed grid line runs past the tag down to a wall: no leader."""
    W, H = 1400, 1000
    im = Image.new("L", (W, H), 245)
    d = ImageDraw.Draw(im)
    d.rectangle([100, 800, 1300, 806], fill=40)  # a wall below
    for y in range(100, 800, 24):  # dashed grid line through the tag's column
        d.line([(452, y), (452, y + 14)], fill=120, width=1)
    p = tmp_path / "grid.png"
    im.save(p)
    tl = T.TextLayer(path=str(p), size=(W, H), items=[_it("Dining", 400, 400, 450, 425), _it("Void Above", 390, 425, 470, 450)])
    assert M.leader_tip(str(p), tl, tl.items[0], 29.5) is None


def test_a_count_that_stopped_early_is_recounted_from_the_labels():
    texts = ["CENTER TANK N 9", "CENTER TANK N8", "CENTER TANK N97", "CENTER TANK N96", "CENTER TANK NS",
             "CENTER TANK M93", "CENTER TANK Nº4", "CENTER TANK N 2", "WING TANK N 4 P/S", "CORRUGATED CARGO TANK",
             "CENTER TANK PIPING SCHEMATIC SHOWING ALL VALVES"]
    items = [T.TextItem(t, [100.0 * k, 1969.0, 100.0 * k + 76, 1984.0]) for k, t in enumerate(texts)]
    items[-1].box = [0.0, 50.0, 900.0, 90.0]  # a title, printed larger
    tl = T.TextLayer(path="x.png", size=(3000, 2319), items=items)
    q = "How many centre tanks are there on the cargo tank piping?"
    n, got, name = Q._label_recount(6, q, items[:6], tl)
    assert n == 8 and name == "CENTER TANK" and all(i.text.startswith("CENTER TANK N") or "M93" in i.text for i in got)
    assert Q._label_recount(8, q, items[:8], tl) is None  # nothing missed
    assert Q._label_recount(6, "How many wing tanks are there?", items[:6], tl) is None
    assert Q._label_recount(6, "What is the capacity of the centre tanks?", items[:6], tl) is None
    assert Q._label_recount(6, "How many centre tanks are there, not counting tank 9?", items[:6], tl) is None
    assert Q._label_recount(6, "How many forward centre tanks are there?", items[:6], tl) is None
    assert Q._label_recount(6, "What is the number of centre tanks?", items[:6], tl)[0] == 8


def test_counts_sums_and_differences_are_computed_on_the_cited_lines():
    it = lambda t: T.TextItem(t, [0, 0, 1, 1])  # noqa: E731
    chk = lambda v, u, q, c: Q._arith_check(v, u, q, [it(t) for t in c])  # noqa: E731
    # D1 22:31 (HAER hi76 sheets): a range, a list continued on the next line, a sum that dropped a term
    assert chk(8, "count", "How many overhead pipe support structures are to be stabilized?",
               ["PIPE SUPPORT STRUCTURES OPS3-OPS8, SEE DETAILS, SHT. S8."])[0] == 6
    assert chk(6, "count", "How many grid lines get 2x4 blocking between the purlins?",
               ["ALONG GRIDS 1,3,4,8,10,12", "14,16,18,20,22, AND 24, TYP."])[0] == 12
    assert chk(2, "count", "How many tanks are to be removed?", ["REMOVE TANKS T13-18, AND 20"])[0] == 7
    assert chk(6, "count", "How many 4x6 posts get a splice repair?",
               ["CATIONS: B-3, B-4, D-13, C-15, C-16 AND E-16."]) is None  # 6 = the list (sentence-final '.')
    assert chk("1,500,000 gallons", "gallons", "What is the combined nominal capacity of tanks T5, T18 and T19?",
               ["500,000 GALLON", "750,000 GALLON", "750,000, GALLON"])[0] == 2000000
    got = chk("1.5 feet", "ft", "How much higher is the floor of tank T5 than the floor of tank T18?",
              ["TANK CENTER POINT TABLE", "ENGINEERING RECORD"])
    assert got[0] is None and got[2] <= 0.4
    assert chk(1.5, "ft", "How much higher is the floor of tank T5 than tank T18?", ["FF=4000.00", "FF=3995.17"])[0] == 4.83
    assert chk(3568100, None, "What is the total raw water storage capacity?",
               ["TOTAL", "626,600 GALLONS", "= 3,568,100 GALLONS"]) is None  # a printed total
    assert chk(3, "count", "How many WCs are there?", ["WC", "WC", "WC"]) is None
    assert chk(7, "count", "How many bays are there?", ["7,200", "7,200", "7,200"]) is None  # thousands, not a list
    assert chk(1.0, "m", "How much larger is catchment GC1 than GC2?", ["CATCHMENT - GC1", "CATCHMENT - GC2"])[2] <= 0.4


def test_an_enumeration_of_other_items_stays_unsure():
    it = lambda t: T.TextItem(t, [0, 0, 1, 1])  # noqa: E731
    got = Q._arith_check(10, "count", "How many existing redwood tanks are there?", [it("REMOVE TANKS T13-18, AND 20")])
    assert got[0] == 7 and got[2] <= 0.4  # the cited line lists the tanks to REMOVE (D1: gold 13 is another line)
    assert Q._arith_check(2, "count", "How many tanks are to be removed?", [it("REMOVE TANKS T13-18, AND 20")])[2] == 0.6


def test_stations_are_feet_along_the_route():
    it = lambda t: T.TextItem(t, [0, 0, 1, 1])  # noqa: E731
    assert Q._station("3+20 STA") == 320 and Q._station("STA. 55+60") == 5560 and Q._station("7+24.25") == 724.25
    assert Q._station("1+2") is None and Q._station("12+345") is None
    got = Q._arith_check(230, "ft", "What is the difference between STA. 55+60 and STA. 53+00?",
                         [it("AIR RELIEF VALVE STA. 55+60"), it("BLOW OFF STA. 53+00")])
    assert got[0] == 260 and got[2] == 0.6  # D1 22:52: 230 @0.75, gold 260


def test_several_enumerations_are_ambiguous():
    it = lambda t: T.TextItem(t, [0, 0, 1, 1])  # noqa: E731
    lines = ["4. TANKS T1-T4 ARE EXISTING WELDED STEEL TANKS.", "SEE DETAIL G, SHEET", "REMOVE REDWOOD TANKS T13-T18, T20,"]
    got = Q._arith_check(10, "count", "How many existing redwood tanks are there?", [it(t) for t in lines])
    assert got[0] is None and got[2] <= 0.4
    assert Q._arith_check(7, "count", "How many redwood tanks are to be removed?", [it(t) for t in lines]) is None


def test_an_element_footprint_is_not_the_sheet_overall():
    from stoa.solvers.raster import chains as CH

    assert CH.question_kind("What's the footprint of the generator set foundation?") is None  # T1 22:42 (S-100F)
    assert CH.question_kind("What is the footprint of the building?") == ("area", "footprint")
