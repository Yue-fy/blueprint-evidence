"""O1: OCR text layer (stoa.ocr) and the QA line selection (raster textqa.select_items). No paddle needed."""

import json

from PIL import Image

from stoa.solvers.raster import textlayer as T
from stoa.solvers.raster.textqa import select_items


def _item(text, x, y, w=100, h=20):
    return T.TextItem(text=text, box=[x, y, x + w, y + h])


def test_select_items_keeps_title_block_and_question_words():
    W, H = 3000, 2000
    body = [_item(f"2Ls5x3 {k}", 400 + (k % 20) * 100, 300 + (k // 20) * 40) for k in range(300)]
    title = _item("NEW HAMPSHIRE", 2900, 900)  # right margin
    named = _item("Bellows Falls Arch Bridge", 1200, 1000)  # shares words with the question
    items = body[:150] + [named] + body[150:] + [title]
    got = select_items(items, "What is the name of the bridge? Bellows?", W, H, 160)
    assert len(got) == 160 and title in got and named in got
    assert got == [i for i in items if i in got]  # reading order kept


def test_select_items_short_layer_unchanged():
    items = [_item("A", 0, 0), _item("B", 10, 10)]
    assert select_items(items, "q", 100, 100, 160) == items


def test_ocr_engine_layer_from_cache(tmp_path, monkeypatch):
    from stoa import ocr

    img = tmp_path / "scan.png"
    Image.new("RGB", (400, 300), "white").save(img)
    monkeypatch.setattr(ocr, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("STOA_TEXT_ENGINE", "ocr")
    f = ocr._key(str(img))
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"size": [400, 300], "seconds": 1.5, "engine": "PP-OCRv6_small", "items": [
        {"text": "SCALE 1:100", "box": [10, 250, 120, 270], "score": 0.98, "rot": 0},
        {"text": "noise", "box": [5, 5, 9, 9], "score": 0.2, "rot": 0},
        {"text": "NEW HAMPSHIRE", "box": [380, 50, 395, 200], "score": 0.99, "rot": 270}]}))
    tl = T.text_layer(str(img))
    assert [i.text for i in tl.items] == ["SCALE 1:100", "NEW HAMPSHIRE"]  # score < 0.5 dropped
    assert tl.tokens_in == 0 and tl.calls == 0 and "vertical" in tl.notes[-1]
    assert T.scales(tl) and T.scales(tl)[0]["ratio"] == 100


def test_engine_tiles_and_dedupe():
    from stoa.ocr import engine as E

    assert E._tiles(3000, 2000) == [(0, 0, 3000, 2000)]
    t = E._tiles(9000, 3000)
    assert all(x1 - x0 <= E.MAX_SIDE for x0, _, x1, _ in t) and max(x1 for *_, x1, _ in t) == 9000
    a = {"text": "A", "box": [0, 0, 100, 20], "score": 0.9, "rot": 0}
    b = {"text": "A", "box": [2, 1, 101, 21], "score": 0.8, "rot": 0}
    c = {"text": "C", "box": [500, 0, 600, 20], "score": 0.7, "rot": 0}
    assert [i["text"] for i in E._dedupe([b, a, c])] == ["A", "C"]


def test_fix_text_scale_misreads_parse():
    from stoa.ocr.engine import fix_text

    assert [s["ratio"] for s in T.parse_scales(fix_text('SCALE: 1/4"=I\'-0"'))] == [48]
    assert [s["ratio"] for s in T.parse_scales(fix_text("8CALE 1/8° - f-0°"))] == [96]
    assert [s["ratio"] for s in T.parse_scales(fix_text('SCALE : 3/ 8 " = I\'- 0"'))] == [32]
    assert fix_text("BASEMENT FLOOR PLAN") == "BASEMENT FLOOR PLAN"  # non-scale lines untouched
    assert fix_text("Ilmari") == "Ilmari"


def test_unrotate_maps_back_to_original_frame():
    from stoa.ocr.engine import _unrotate

    # rotate(90) is counter-clockwise: the rotated top-left corner is the original top-right corner
    assert _unrotate([0, 0, 10, 100], 90, 3000, 2000) == [2900, 0, 3000, 10]
    # rotate(270): the rotated top-left corner is the original bottom-left corner
    assert _unrotate([0, 0, 10, 100], 270, 3000, 2000) == [0, 1990, 100, 2000]


def test_auto_engine_keeps_vlm_layer_on_clean_renders(tmp_path, monkeypatch):
    from stoa import ocr

    img = tmp_path / "plan.png"  # pure white + one grey level = a clean render
    im = Image.new("RGB", (400, 300), "white")
    for x in range(50, 350):
        im.putpixel((x, 150), (0, 0, 0))
    im.save(img)
    monkeypatch.setattr(ocr, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")
    called = []
    monkeypatch.setattr(ocr, "text_layer", lambda p: called.append(p))
    monkeypatch.setattr(T, "_read", lambda *a, **k: T.TextLayer(path=str(img), size=(400, 300)))
    monkeypatch.setattr(T, "CACHE_DIR", tmp_path / "vlmcache")
    assert T.is_clean(str(img))
    T.text_layer(str(img))
    assert called == []  # auto never sends a clean render to OCR


def test_fix_text_sheet_of_and_digit_tokens():
    from stoa.ocr.engine import fix_text

    assert fix_text("9 °F 15") == "9 OF 15"
    assert fix_text("KM I6.5 ROUTE") == "KM 16.5 ROUTE"
    assert fix_text("1.1$") == "1.15"
    assert fix_text("Il Duomo") == "Il Duomo"


def test_ocr_suits_large_scans_only(tmp_path):
    import numpy as np

    rng = np.random.default_rng(0)
    big = tmp_path / "scan_big.jpg"  # paper texture, many grey levels, 2400 px: a scanned sheet
    Image.fromarray(rng.integers(150, 256, (1600, 2400, 3), dtype=np.uint8)).save(big)
    small = tmp_path / "scan_small.jpg"  # same texture at CubiCasa size
    Image.fromarray(rng.integers(150, 256, (600, 900, 3), dtype=np.uint8)).save(small)
    assert T.ocr_suits(str(big)) and not T.ocr_suits(str(small))


def test_fix_text_inch_feet_map_scales():
    from stoa.ocr.engine import fix_text

    assert [s["ratio"] for s in T.parse_scales(fix_text('I"=400"'))] == [4800]  # 1" = 400' (tn0101)
    assert [s["ratio"] for s in T.parse_scales(fix_text("1'=12000"))] == [144000]  # 1" = 12,000' (ar0135)


def test_mark_ocr_exact_only_on_ocr_line_boxes(tmp_path, monkeypatch):
    from stoa.contract import Answer, Evidence

    p = str(tmp_path / "s.jpg")
    tl = T.TextLayer(path=p, size=(100, 100), items=[T.TextItem(text="SCALE 1:100", box=[10.0, 20.0, 60.0, 30.0])])
    monkeypatch.setitem(T._mem, f"{p}{T.file_tag(p)}|ocr", tl)
    ans = Answer(value="1:100", solver="raster", evidence=[
        Evidence(file=p, page=0, bbox=[10.0, 20.0, 60.0, 30.0], bbox_space="image_px", note="scale note"),
        Evidence(file=p, page=0, bbox=[0.0, 0.0, 50.0, 50.0], bbox_space="image_px", note="region")])
    out = T.mark_ocr_exact(ans)
    assert [e.exact for e in out.evidence] == [True, None]


def test_photo_misreads_scale():
    from stoa.ocr.engine import fix_text

    assert [s["ratio"] for s in T.parse_scales(fix_text('SCALE:1/4"=I\'-O"'))] == [48]
    assert T.parse_scales("1:51/2") == []  # a dimension 1'-5 1/2" misread, not a 1:51 scale


def test_measure_outline_polygon():
    import numpy as np

    from stoa.solvers.raster.measure import outline

    m = np.zeros((50, 80), np.uint8)
    m[10:30, 20:60] = 1
    poly = outline(m)
    assert poly and len(poly) == 4
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    assert (min(xs), max(xs), min(ys), max(ys)) == (20, 59, 10, 29)
    assert outline(np.zeros((5, 5), np.uint8)) is None


def test_area_labels_misread_as_m3_or_m_on_an_m2_sheet():
    from stoa.ocr import area_units
    from stoa.solvers.raster.textlayer import TextItem

    lines = ["12.05 m²", "18.36 m²", "4.92 m2", "15.61 m³", "10.16m", "Zimmer", "1450 m³"]
    out, n = area_units([TextItem(t, [0, 0, 1, 1]) for t in lines])
    assert n == 2 and out[3].text == "15.61 m²" and out[4].text == "10.16 m²" and out[6].text == "1450 m³"
    few, n2 = area_units([TextItem(t, [0, 0, 1, 1]) for t in ["12.05 m²", "10.16m"]])
    assert n2 == 0 and few[1].text == "10.16m"  # too few area labels: a length stays a length
