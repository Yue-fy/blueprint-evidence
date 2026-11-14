"""F1 region ask (stoa/features/region.py): geometry, masked copies, scoping strategies, flag, router."""

from __future__ import annotations

import pymupdf
import pytest
from PIL import Image

from stoa.contract import Answer, Evidence
from stoa.features import region as R


def _pdf(tmp_path, rotate: int = 0):
    """A 600 x 400 pt page: 'Kitchen' + a line inside x 100-300 / y 100-300, 'Bedroom' + a line outside, a scale note."""
    p = tmp_path / "plan.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=600, height=400)
    pg.insert_text((150, 200), "Kitchen", fontsize=10)
    pg.insert_text((450, 200), "Bedroom", fontsize=10)
    pg.insert_text((450, 380), "SCALE 1:100", fontsize=8)
    pg.draw_line((120, 120), (280, 120))  # inside
    pg.draw_line((420, 50), (580, 50))  # outside
    pg.draw_line((20, 20), (20, 390))  # outside, long, left of the region
    if rotate:
        pg.set_rotation(rotate)
    d.save(p)
    return p


def test_parse_region_box_and_invalid():
    r = R.parse_region({"page": 1, "space": "pdf_pt", "polygon": [[300, 300], [100, 100]]})
    assert r.bbox == (100, 100, 300, 300) and r.is_box and r.page == 1
    assert R.parse_region({"bbox": [0, 0, 10, 20], "space": "image_px"}).space == "image_px"
    assert R.parse_region(None) is None
    assert R.parse_region({"polygon": [[0, 0]]}) is None
    assert R.parse_region({"bbox": [0, 0, 10, 10], "space": "svg"}) is None
    lasso = R.parse_region({"polygon": [[0, 0], [10, 0], [5, 10]]})
    assert not lasso.is_box and lasso.contains(5, 3) and not lasso.contains(9, 9)


def test_complement_covers_the_page_minus_keeps():
    page, keeps = (0, 0, 100, 100), [(10, 10, 40, 40), (60, 60, 90, 80)]
    rects = R.complement(page, keeps)
    area = sum((c - a) * (d - b) for a, b, c, d in rects)
    assert area == pytest.approx(100 * 100 - 30 * 30 - 30 * 20)
    for a, b, c, d in rects:  # no output rect overlaps a keep
        for k in keeps:
            assert min(c, k[2]) - max(a, k[0]) <= 1e-9 or min(d, k[3]) - max(b, k[1]) <= 1e-9


def test_scoped_question():
    assert R.scoped_question("How many doors are in this area?") == "How many doors are on this plan?"
    assert R.scoped_question("How many windows in here?") == "How many windows on this plan?"
    assert R.scoped_question("Total area of the rooms in the selected region") == "Total area of the rooms on this plan"
    assert R.scoped_question("What is the kitchen's area?") == "What is the kitchen's area?"


@pytest.mark.parametrize("rotate", [0, 90])
def test_mask_pdf_keeps_region_and_scale(tmp_path, rotate):
    p = _pdf(tmp_path, rotate)
    with pymupdf.open(p) as d:  # the region in the CONTRACT space (unrotated pdf_pt) around 'Kitchen'
        w = [w for w in d[0].get_text("words") if w[4] == "Kitchen"][0]
    x, y = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
    reg = R.parse_region({"page": 0, "space": "pdf_pt", "bbox": [x - 60, y - 60, x + 60, y + 60]})
    m = R.masked_copy(str(p), reg)
    with pymupdf.open(m.path) as d:
        pg = d[0]
        words = {w[4] for w in pg.get_text("words")}
        assert pg.rotation == rotate
        n_draw = len(pg.get_drawings())
    assert "Kitchen" in words and "Bedroom" not in words
    assert "1:100" in words and m.kept_scale  # the printed scale stays for the readers
    assert n_draw < 3  # the two lines outside are gone
    assert m.n_text_in == 1


def test_mask_image_whites_outside(tmp_path):
    p = tmp_path / "plan.png"
    Image.new("RGB", (200, 100), (0, 0, 0)).save(p, dpi=(150, 150))
    reg = R.parse_region({"space": "image_px", "polygon": [[10, 10], [90, 10], [90, 90], [10, 90]]})
    m = R.masked_copy(str(p), reg)
    with Image.open(m.path) as im:
        assert im.size == (200, 100)
        assert im.getpixel((50, 50)) == (0, 0, 0) and im.getpixel((150, 50)) == (255, 255, 255)
        assert round(im.info["dpi"][0]) == 150


def test_with_region_off_is_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    calls = []
    out = R.with_region("x.pdf", "q?", {"bbox": [0, 0, 5, 5]}, lambda p, q: calls.append((p, q)) or "same")
    assert out == "same" and calls == [("x.pdf", "q?")]


def test_with_region_masked_count_drops_marks_outside(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    seen = {}

    def solve(path, q):
        seen["path"], seen["q"] = path, q
        ev = [Evidence(file=path, page=0, bbox=[140, 140, 150, 150], bbox_space="pdf_pt", note="door 1"),
              Evidence(file=path, page=0, bbox=[500, 300, 510, 310], bbox_space="pdf_pt", note="door 2")]
        return Answer(value=2, unit="count", confidence=0.7, evidence=ev, solver="pipeline/openings")

    a = R.with_region(str(p), "How many doors are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert seen["path"] != str(p) and ".region-" in seen["path"]
    assert seen["q"] == "How many doors are on this plan?"
    assert a.value == 1
    assert all(e.file == str(p) for e in a.evidence)
    assert any("scoped to" in s for s in a.assumptions)
    assert a.evidence[0].note == "the region you drew"


def test_with_region_rooms_by_labels_checks_the_outline(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    monkeypatch.setattr(R, "ask_by_tags", lambda *a, **k: None)

    def solve(path, q):
        assert path == str(p)  # labels are asked on the FULL sheet
        if "Kitchen" in q:
            return Answer(value=12.5, unit="m2", confidence=0.55, solver="pipeline/plan_rooms",
                          evidence=[Evidence(file=path, page=0, bbox=[120, 120, 280, 280], bbox_space="pdf_pt",
                                             polygon=[[120, 120], [280, 120], [280, 280], [120, 280]])])
        return Answer(value=None, confidence=0.0)

    a = R.with_region(str(p), "What is the total area of the rooms in here?", {"page": 0, "bbox": [100, 100, 300, 300]},
                      solve)
    assert a.value == 12.5 and a.solver == "region/labels" and a.unit == "m2"
    assert any("Kitchen 12.5" in s for s in a.assumptions)


def test_label_read_elsewhere_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    reg = R.parse_region({"page": 0, "bbox": [100, 100, 300, 300]})
    labels = R.room_labels(str(p), reg)
    assert [lab["text"] for lab in labels] == ["Kitchen"]

    def solve(path, q):  # a same-named room OUTSIDE the region
        return Answer(value=9.0, unit="m2", confidence=0.6,
                      evidence=[Evidence(file=path, page=0, bbox=[420, 150, 580, 250], bbox_space="pdf_pt")])

    ans, rejected = R.ask_by_labels(str(p), "total area here", reg, solve, labels)
    assert ans is None and "outside the region" in rejected[0]


def test_failure_falls_back_to_whole_sheet_low_confidence(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    f = tmp_path / "model.ifc"
    f.write_text("ISO-10303-21;")
    a = R.with_region(str(f), "How many doors here?", {"bbox": [0, 0, 5, 5]},
                      lambda p, q: Answer(value=7, unit="count", confidence=0.9))
    assert a.value == 7 and a.confidence == 0.2 and "could not limit" in a.missing[-1]


def test_router_health(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STOA_FEATURES", "region")
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app)
    assert c.get("/feat/region/health").json() == {"ok": True, "feature": "region", "enabled": True}


def test_upload_solve_honours_a_drawn_region(tmp_path, monkeypatch):
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    p = _pdf(tmp_path)
    plain = lambda path, q: Answer(value=3, unit="count", confidence=0.7, solver="x")  # noqa: E731
    assert R.upload_solve(plain, None) is plain and R.upload_solve(plain, {"point": [1, 2]}) is plain
    s = R.upload_solve(plain, {"region": [[100, 100], [300, 100], [300, 300], [100, 300]], "space": "pdf_pt", "page": 0})
    a = s(str(p), "How many doors are in this area?")
    assert a.evidence[0].note == "the region you drew" and any("scoped to" in x for x in a.assumptions)


def test_outline_area_at_the_printed_scale(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)  # prints 'SCALE 1:100': 1 pt = 0.0254 / 72 * 100 m
    a = R.with_region(str(p), "What is the area of this outline?", {"page": 0, "bbox": [100, 100, 172, 136]},
                      lambda *_: (_ for _ in ()).throw(AssertionError("no reader for an outline")))
    m = 0.0254 / 72 * 100
    assert a.solver == "region/outline" and a.unit == "m2"
    assert a.value == pytest.approx(72 * 36 * m * m, abs=0.006)
    assert a.evidence[0].polygon and a.evidence[0].exact
    per = R.with_region(str(p), "What is the perimeter of this outline?", {"page": 0, "bbox": [100, 100, 172, 136]},
                        lambda *_: None)
    assert per.unit == "m" and per.value == pytest.approx(2 * (72 + 36) * m, abs=0.006)


def test_outline_without_scale_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = tmp_path / "plan.png"
    Image.new("RGB", (200, 100), (255, 255, 255)).save(p)
    a = R.with_region(str(p), "area of my outline", {"space": "image_px", "bbox": [10, 10, 60, 60]}, lambda *_: None)
    assert a.value is None and "scale" in a.missing[0]


def test_router_ask_and_suggest_take_a_json_body(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STOA_FEATURES", "region")
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app)
    r = c.post("/feat/region/suggest", json={"path": "data/../x.pdf", "region": {"bbox": [0, 0, 5, 5]}})
    assert r.status_code == 400  # the body was parsed (422 = it was read as a query parameter)


def test_upload_solve_hides_the_region_from_the_inner_pipeline(tmp_path, monkeypatch):
    import json as _json

    p = _pdf(tmp_path)
    monkeypatch.setenv("STOA_ASK_CONTEXT", _json.dumps({"region": [[1, 1], [2, 1], [2, 2]], "space": "pdf_pt", "page": 0}))
    seen = []

    def plain(path, q):
        seen.append(_json.loads(__import__("os").environ["STOA_ASK_CONTEXT"]))
        return Answer(value=1, unit="count", confidence=0.7)

    s = R.upload_solve(plain, {"region": [[100, 100], [300, 100], [300, 300], [100, 300]], "space": "pdf_pt", "page": 0})
    s(str(p), "How many doors are in this area?")
    assert seen and all("region" not in c for c in seen) and seen[0]["space"] == "pdf_pt"
    assert "region" in _json.loads(__import__("os").environ["STOA_ASK_CONTEXT"])  # restored afterwards


# ------------------------------------------------------------------------------------- cycle 2: wall snap + baseboard

def _room_png(tmp_path):
    """50 px = 1 m. A room with 6-px filled walls, inner faces x 100-400 / y 100-350 (6 x 5 m, perimeter 22 m); a 1-m
    door gap in the left wall (y 200-250) with a thin sill line drawn across it; a window (two thin glazing lines) in
    the top wall. Baseboard = 22 - 1 = 21 m."""
    import numpy as np

    a = np.full((460, 520, 3), 255, np.uint8)
    a[94:100, 94:406] = 0  # top wall
    a[350:356, 94:406] = 0  # bottom
    a[94:356, 400:406] = 0  # right
    a[94:200, 94:100] = 0  # left, above the door
    a[250:356, 94:100] = 0  # left, below the door
    a[200:250, 96] = 0  # thin sill line across the doorway (not a wall)
    a[94:100, 200:300] = 255  # window opening in the top wall ...
    a[95, 200:300] = 0  # ... with two glazing lines (a wall for skirting)
    a[98, 200:300] = 0
    p = tmp_path / "room.png"
    Image.fromarray(a).save(p)
    return p


def test_snap_outline_moves_sides_onto_wall_faces(tmp_path):
    p = _room_png(tmp_path)
    reg = R.parse_region({"space": "image_px", "polygon": [[108, 92], [392, 109], [409, 341], [91, 357]]})
    a = R.outline_measure(str(p), "What is the area of this outline?", reg, user_px_per_m=50)
    assert a.unit == "m2" and abs(a.value - 30.0) / 30.0 < 0.02  # 6 x 5 m from a sloppy outline
    assert "on the wall faces" in a.evidence[0].note and "snapped" in a.trace[0]
    exact = R.outline_measure(str(p), "What is the area of this outline, as drawn?", reg, user_px_per_m=50)
    assert "on the wall faces" not in exact.evidence[0].note  # after a corner drag: measured exactly as placed
    assert abs(exact.value - R._shoelace(reg.polygon) / 2500) < 0.01


def test_baseboard_deducts_the_door_not_the_window_or_sill(tmp_path):
    p = _room_png(tmp_path)
    reg = R.parse_region({"space": "image_px", "polygon": [[104, 96], [396, 104], [404, 346], [96, 354]]})
    a = R.outline_measure(str(p), "Baseboard length of this outline (door openings excluded)?", reg, user_px_per_m=50)
    assert a.unit == "m" and abs(a.value - 21.0) / 21.0 < 0.02, a.trace
    gaps = [e for e in a.evidence if e.polyline]
    assert len(gaps) == 1 and "(no skirting)" in gaps[0].note  # the viewer draws it dashed as a deducted opening
    assert 0.85 <= float(gaps[0].note.split()[1]) <= 1.05
    assert any("perimeter" in t and "opening" in t for t in a.trace)
    assert R._OUTLINE_Q.search("Baseboard length of this outline as drawn (door openings excluded)?")


def test_baseboard_outline_off_the_walls_deducts_nothing(tmp_path):
    p = _room_png(tmp_path)  # an outline 1.5 m inside the room: no wall within reach on any side
    reg = R.parse_region({"space": "image_px", "polygon": [[175, 175], [325, 175], [325, 275], [175, 275]]})
    a = R.outline_measure(str(p), "Baseboard length of this outline?", reg, user_px_per_m=50)
    assert a.value == pytest.approx(R._perimeter(reg.polygon) / 50, abs=0.01)
    assert a.missing and a.confidence <= 0.3


def test_freehand_lasso_is_simplified_then_snapped(tmp_path):
    import math

    p = _room_png(tmp_path)  # a wobbly 60-point lasso around the room, ~0.1-0.2 m off the walls
    pts = []
    for k in range(60):
        t = k / 60
        if t < 0.25:
            x, y = 106 + (394 - 106) * t / 0.25, 104
        elif t < 0.5:
            x, y = 394, 104 + (346 - 104) * (t - 0.25) / 0.25
        elif t < 0.75:
            x, y = 394 - (394 - 106) * (t - 0.5) / 0.25, 346
        else:
            x, y = 106, 346 - (346 - 104) * (t - 0.75) / 0.25
        pts.append([x + 3 * math.sin(k * 1.7), y + 3 * math.cos(k * 2.3)])
    reg = R.parse_region({"space": "image_px", "polygon": pts})
    a = R.outline_measure(str(p), "What is the area of this outline?", reg, user_px_per_m=50)
    assert "on the wall faces" in a.evidence[0].note and abs(a.value - 30.0) / 30.0 < 0.02, a.trace


def test_faint_grey_window_glazing_is_a_wall_not_an_opening(tmp_path):
    """Scans render a window's glazing as thin GREY lines (DfE hall, g ~ 180): two of them across the wall band keep the
    skirting running; a single thin line (a sill) does not."""
    import numpy as np

    p = _room_png(tmp_path)
    a = np.asarray(Image.open(p).convert("RGB")).copy()
    a[94:100, 200:300] = 255
    a[95, 200:300] = 180  # faint glazing lines
    a[98, 200:300] = 175
    Image.fromarray(a).save(p)
    reg = R.parse_region({"space": "image_px", "polygon": [[100, 100], [400, 100], [400, 350], [100, 350]]})
    ans = R.outline_measure(str(p), "Baseboard length of this outline?", reg, user_px_per_m=50)
    gaps = [e for e in ans.evidence if e.polyline]
    assert len(gaps) == 1 and abs(ans.value - 21.0) / 21.0 < 0.02, ans.trace
