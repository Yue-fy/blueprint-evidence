"""O1 c8: the OCR quality gate (stoa.ocr.quality + textlayer._ocr_gate)."""
import pytest

from stoa.ocr import quality as Q
from stoa.solvers.raster import textlayer as T

needs_dict = pytest.mark.skipif(Q.words() is None, reason="no English word list on this machine")


def _items(texts, score=0.88):
    return [{"text": t, "score": score} for t in texts]


GARBLED = ["Faress Roons", "Prant Poom", "Secondiry Chatsroom", "Seoundary Classmom", "Kitchen", "Laundry",
           "Meatin Roam", "Ertranca Lobby", "Corridor", "Grovp Ruom", "Hygeine Suit", "Stafl Rocm"] * 2
CLEAN = ["Fitness Room", "Plant Room", "Secondary Classroom", "Kitchen", "Laundry", "Meeting Room",
         "Entrance Lobby", "Corridor", "Group Room", "Hygiene Suite", "Staff Room", "Store"] * 2


@needs_dict
def test_judge_garbled_vs_clean():
    g = Q.judge(_items(GARBLED))
    c = Q.judge(_items(CLEAN))
    assert g.tokens >= Q.MIN_TOKENS and g.garbled and g.share < 0.6
    assert not c.garbled and c.share > 0.9


@needs_dict
def test_judge_keeps_confident_foreign_and_short_layers():
    swiss = ["Wohnen Essen", "Schlafen", "Kochen", "Reduit", "Zimmer", "Nasszelle", "Abstellraum", "Korridor",
             "Treppenhaus", "Waschküche", "Estrich", "Gartensitzplatz"] * 3
    assert not Q.judge(_items(swiss, score=0.97)).garbled  # read confidently: another language, not garbage
    assert not Q.judge(_items(GARBLED[:5])).garbled  # too few tokens to judge
    finnish = ["Olohuone", "Makuuhuone", "Keittiö", "Kylpyhuone", "Eteinen", "Vaatehuone"] * 6
    finnish = [f + " ja" for f in finnish] + ["Asuinkerrostalo Pohjapiirustus Mittakaava Rakennuslupa"] * 6
    assert not Q.judge(_items(finnish, score=0.85)).garbled


def test_gate_off_switch(monkeypatch):
    monkeypatch.setenv("STOA_O1_OCR_GATE", "0")
    assert Q.judge(_items(GARBLED)) is None


@needs_dict
def test_text_layer_takes_the_better_reading(monkeypatch, tmp_path):
    from stoa import ocr

    img = tmp_path / "scan.png"
    from PIL import Image

    Image.new("L", (2400, 1600), 200).save(img)
    p = str(img)

    def mk(texts):
        return T.TextLayer(path=p, size=(2400, 1600),
                           items=[T.TextItem(t, [10.0 * k, 10.0, 10.0 * k + 50, 30.0]) for k, t in enumerate(texts)])

    monkeypatch.setattr(T, "ocr_suits", lambda path: True)
    monkeypatch.setattr(ocr, "cached", lambda path: True)
    monkeypatch.setattr(ocr, "text_layer", lambda path: mk(GARBLED))
    monkeypatch.setattr(ocr, "verdict", lambda path: Q.judge(_items(GARBLED)))
    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")
    monkeypatch.setattr(T, "_vlm_layer", lambda *a, **k: mk(CLEAN))
    T._mem.clear()
    tl = T.text_layer(p)
    assert [i.text for i in tl.items] == CLEAN and "not PP-OCR" in tl.notes[-1]
    assert f"{p}{T.file_tag(p)}|ocr" not in T._mem  # mark_ocr_exact must not mark the VLM's boxes exact
    assert T.text_layer(p) is tl  # memoised

    T._mem.clear()
    monkeypatch.setattr(T, "_vlm_layer", lambda *a, **k: mk(GARBLED))  # the VLM is no better: keep the OCR
    tl = T.text_layer(p)
    assert "OCR kept" in tl.notes[-1] and f"{p}{T.file_tag(p)}|ocr" in T._mem
    T._mem.clear()


def test_join_tile_cuts():
    a = T.TextItem("MODULE 01- ENTRY VESTIE", [100.0, 50.0, 400.0, 70.0])
    b = T.TextItem("VESTIBULE - FLOOR PLAN", [330.0, 51.0, 620.0, 71.0])
    c = T.TextItem("6,285", [100.0, 200.0, 160.0, 220.0])
    d = T.TextItem(",285 SF", [128.0, 200.0, 210.0, 220.0])
    e = T.TextItem("KITCHEN", [700.0, 50.0, 800.0, 70.0])  # same baseline, no overlap: kept apart
    out, n = T.join_tile_cuts([a, b, c, d, e])
    texts = sorted(i.text for i in out)
    assert n == 2 and texts == ["6,285 SF", "KITCHEN", "MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"]


def test_thousands_and_no_deduct():
    from stoa.solvers.raster import textqa as X

    cited = [T.TextItem("WEST WING = 7,009 SF", [0.0, 0.0, 1.0, 1.0])]
    assert X._thousands("7,009", "ft2", "West wing area?", [])
    assert X._thousands("7,009", None, "West wing area?", cited)
    assert X._thousands("4,479", None, "How many square feet of circulation are there?", [])
    assert not X._thousands("20,5", "m2", "Area of the kitchen?", [])
    assert not X._thousands("3,450", "m", "How long is the wall in the kitchen?", [])
    assert X._NO_DEDUCT.search("How many metres of skirting? Measure the full perimeter, do not deduct door openings.")
    assert X._NO_DEDUCT.search("Skirting for the hall, ignoring door openings")
    assert not X._NO_DEDUCT.search("How many metres of skirting does the laundry need?")


def test_elevation_title_with_printed_rooms_is_not_vetoed():
    """HABS 215 Abingdon Ave: elevation titles only, but a plan with room names -> the text veto defers (None)."""
    from stoa.solvers.raster import sheet as S

    items = [T.TextItem("Front (North) Elevation", [100.0, 700.0, 400.0, 740.0]),
             T.TextItem("LIVING ROOM", [100.0, 100.0, 200.0, 120.0]), T.TextItem("KITCHEN?", [300.0, 100.0, 380.0, 120.0]),
             T.TextItem("BEDROOM?", [100.0, 300.0, 190.0, 320.0])]
    tl = T.TextLayer(path="x.jpg", size=(1000, 800), items=items)
    assert S.room_labels(tl) == ["LIVING ROOM", "KITCHEN?", "BEDROOM?"]
    assert S.sheet_kind("x.jpg", use_vlm=False, layer=tl)["has_plan"] is None
    tl.items = items[:2]
    assert S.sheet_kind("x.jpg", use_vlm=False, layer=tl)["has_plan"] is False


def test_level_annotation_is_no_area():
    from stoa.solvers.raster import textqa as X

    def it(t):
        return T.TextItem(t, [0.0, 0.0, 1.0, 1.0])

    assert X._level_cited(10000.0, [it("EL. = 10000")])  # H 18:54: S-100F '150 mm slab' -> 10000 m² @0.75
    assert X._level_cited(3.2, [it("FFL +3.200")]) and X._level_cited(98.5, [it("T.O.S. 98.50")])
    assert not X._level_cited(61.69, [it("150 SLAB 61.69 m2")]) and not X._level_cited(12.5, [it("LEVEL 2 12.5 m²")])


def test_inch_mark_in_cited_text():
    from stoa.solvers.raster import textqa as X

    def c(t):
        return [T.TextItem(t, [0.0, 0.0, 1.0, 1.0])]

    assert X._inch_cited("16", c('16" SUCTION HEADER')) and X._inch_cited("12", c("12'' DISCHARGE"))
    assert not X._inch_cited("60", c("60'-0\"")) and not X._inch_cited("16", c("16' x 12'"))


@needs_dict
def test_richer_cached_vlm_layer_is_kept(monkeypatch, tmp_path):
    """P1 20:05: an OCR reading that lands after the first question must not replace a clearly richer VLM layer."""
    from PIL import Image

    from stoa import ocr

    img = tmp_path / "scan2.png"
    Image.new("L", (2400, 1600), 200).save(img)
    p = str(img)

    def mk(texts):
        return T.TextLayer(path=p, size=(2400, 1600),
                           items=[T.TextItem(t, [10.0 * k, 10.0, 10.0 * k + 50, 30.0]) for k, t in enumerate(texts)])

    monkeypatch.setattr(T, "ocr_suits", lambda path: True)
    monkeypatch.setattr(ocr, "cached", lambda path: True)
    monkeypatch.setattr(ocr, "text_layer", lambda path: mk(CLEAN[:8]))
    monkeypatch.setattr(ocr, "verdict", lambda path: Q.judge(_items(CLEAN[:8], score=0.95)))
    monkeypatch.setattr(T, "_vlm_cached", lambda *a, **k: True)
    monkeypatch.setattr(T, "_vlm_layer", lambda *a, **k: mk(CLEAN * 2))
    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")
    T._mem.clear()
    assert len(T.text_layer(p).items) == 2 * len(CLEAN)
    T._mem.clear()
    monkeypatch.setattr(T, "_vlm_cached", lambda *a, **k: False)  # nothing read yet: no cold VLM read, OCR stays
    assert len(T.text_layer(p).items) == 8
    T._mem.clear()


def test_union_layer_adds_only_where_ocr_read_nothing():
    from stoa.solvers.raster import textlayer as T

    ocr_tl = T.TextLayer(path="x.png", size=(1000, 800), items=[
        T.TextItem("Kitchen", [100, 100, 160, 115]), T.TextItem("69.600", [300, 300, 350, 312])])
    vlm_tl = T.TextLayer(path="x.png", size=(1000, 800), items=[
        T.TextItem("Kitchen", [104, 121, 164, 136]),  # the same print, box shifted by a line height
        T.TextItem("Kitchn", [110, 102, 150, 113]),  # centre inside the OCR box
        T.TextItem("97.630", [500, 500, 560, 512]),  # the OCR read nothing there
        T.TextItem("-", [700, 700, 705, 705]),  # no digit, < 3 letters
        T.TextItem("Store", [800, 100, 840, 112])])
    u, n = T.union_layer(ocr_tl, vlm_tl)
    assert n == 2
    assert sorted(i.text for i in u.items) == ["69.600", "97.630", "Kitchen", "Store"]
    assert [i.box for i in ocr_tl.items if i.text == "Kitchen"] == [[100, 100, 160, 115]]  # OCR boxes untouched
    assert any("2 text(s) of the VLM reading" in n_ for n_ in u.notes)


def test_union_layer_scales_vlm_boxes_to_the_ocr_image():
    from stoa.solvers.raster import textlayer as T

    ocr_tl = T.TextLayer(path="x.png", size=(2000, 1600), items=[T.TextItem("A", [0, 0, 10, 10])])
    vlm_tl = T.TextLayer(path="x.png", size=(1000, 800), items=[T.TextItem("12.5", [100, 100, 120, 110])])
    u, n = T.union_layer(ocr_tl, vlm_tl)
    assert n == 1 and [i.box for i in u.items if i.text == "12.5"] == [[200.0, 200.0, 240.0, 220.0]]


def test_ocr_server_skipped_when_private_mode_blocks_its_url(monkeypatch):
    import stoa.llm as L
    from stoa import ocr

    def block(url, what="service"):
        raise RuntimeError("private mode: not called")

    monkeypatch.setattr(L, "check_private_url", block, raising=False)
    monkeypatch.setattr(ocr, "_server_ok", True)
    assert ocr.server_up() is False
    monkeypatch.setattr(L, "check_private_url", lambda url, what="service": None, raising=False)
    assert ocr.server_up() is True
