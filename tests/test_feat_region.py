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
    assert R.scoped_question("How many WCs are inside my outline?") == "How many WCs are on this plan?"
    assert R.scoped_question("How many desks within the drawn shape?") == "How many desks on this plan?"
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
    monkeypatch.setenv("STOA_REGION_MARKS", "0")  # the masked copy (the whole sheet's marks: next tests)
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
    assert a.evidence[0].note in ("the box you drew", "the outline you drew")


def test_with_region_count_from_the_whole_sheets_marks(tmp_path, monkeypatch):
    """A count whose whole-sheet answer marks every item: the marks more than half inside the box are the count, asked
    once on the ORIGINAL file (warm caches), no masked copy."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    calls = []

    def solve(path, q):
        calls.append((path, q))
        ev = [Evidence(file=path, page=0, bbox=[140, 140, 150, 150], bbox_space="pdf_pt", note="door 1"),
              Evidence(file=path, page=0, bbox=[295, 200, 315, 210], bbox_space="pdf_pt", note="door 2"),  # 1/4 inside
              Evidence(file=path, page=0, bbox=[285, 250, 305, 260], bbox_space="pdf_pt", note="door 3"),  # 3/4 inside
              Evidence(file=path, page=0, bbox=[500, 300, 510, 310], bbox_space="pdf_pt", note="door 4"),
              Evidence(file=path, page=0, bbox=[90, 90, 520, 320], bbox_space="pdf_pt", note="view: GROUND FLOOR PLAN")]
        return Answer(value=4, unit="count", confidence=0.9, evidence=ev, solver="pipeline/openings")

    a = R.with_region(str(p), "How many doors are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert calls == [(str(p), "How many doors are on this plan?")]
    assert a.value == 2 and a.solver == "region/marks:pipeline/openings" and a.confidence <= 0.85
    assert [e.note for e in a.evidence] == ["the box you drew", "door 1", "door 3"]
    assert "2 of 4 on the sheet" in a.assumptions[0]
    assert any("edge" in m for m in a.missing)


def test_with_region_count_by_a_text_reader_uses_the_masked_copy(tmp_path, monkeypatch):
    """1 pump read by the general agent with ONE note box: not a per-item mark, whatever the numbers say."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    calls = []

    def solve(path, q):
        calls.append(path)
        ev = [Evidence(file=path, page=0, bbox=[140, 140, 150, 150], bbox_space="pdf_pt", note="P-1 pump (legend)")]
        return Answer(value=1, unit="count", confidence=0.6, evidence=ev, solver="pipeline/agent")

    R.with_region(str(p), "How many pumps are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert len(calls) == 2 and ".region-" in calls[1]


def test_with_region_room_count_by_printed_names(tmp_path, monkeypatch):
    """'How many rooms are in this area?' = the room names printed in the box (no reader asked)."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)

    def solve(path, q):
        raise AssertionError("no reader is asked for a room count by names")

    a = R.with_region(str(p), "How many rooms are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert a.value == 1 and a.solver == "region/room_labels" and a.unit == "count"
    assert [e.note for e in a.evidence] == ["the box you drew", "name: Kitchen"]
    assert "1 room named in your box: Kitchen" in a.assumptions[0]


def test_with_region_count_not_one_mark_per_item_uses_the_masked_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    calls = []

    def solve(path, q):
        calls.append(path)
        ev = [Evidence(file=path, page=0, bbox=[140, 140, 150, 150], bbox_space="pdf_pt", note="door schedule")]
        return Answer(value=12, unit="count", confidence=0.8, evidence=ev, solver="pipeline/schedule")

    a = R.with_region(str(p), "How many doors are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert len(calls) == 2 and calls[0] == str(p) and ".region-" in calls[1]
    assert any("scoped to" in s for s in a.assumptions)


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


def test_weak_unchecked_room_is_not_added_to_checked_rooms(tmp_path, monkeypatch):
    """DfE PDF: 'Corridor' -> 120 m² @0.05 (general agent, no outline) beside three checked rooms: left out, said so."""
    p = _pdf(tmp_path)
    reg = R.parse_region({"page": 0, "bbox": [100, 100, 300, 300]})
    labels = [{"text": "Kitchen", "box": [180, 190, 220, 205]}, {"text": "Corridor", "box": [120, 260, 170, 275]}]

    def solve(path, q):
        if "Kitchen" in q:
            return Answer(value=12.5, unit="m2", confidence=0.8, solver="pipeline/plan_rooms",
                          evidence=[Evidence(file=path, page=0, bbox=[120, 120, 280, 250], bbox_space="pdf_pt")])
        return Answer(value=120.0, unit="m2", confidence=0.05, solver="pipeline/agent", evidence=[])

    ans, rejected = R.ask_by_labels(str(p), "What is the total area of the rooms in here?", reg, solve, labels)
    assert ans.value == 12.5 and ans.confidence == 0.8
    assert any("Corridor" in m and "low confidence" in m for m in ans.missing)
    # the weak room alone is still the answer (its low confidence shows)
    ans2, _ = R.ask_by_labels(str(p), "What is the total area of the rooms in here?", reg, solve, labels[1:])
    assert ans2.value == 120.0 and ans2.confidence < 0.1


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
    assert a.evidence[0].note in ("the box you drew", "the outline you drew") and any("scoped to" in x for x in a.assumptions)


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


def test_every_route_refuses_paths_outside_the_dataset_and_uploads(tmp_path, monkeypatch):
    """T2 02:32 (corrections/ask read any absolute path): every region route checks the CLIENT's path at its entry.
    A real drawing outside the dataset / uploads is never opened, whatever form the path takes."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STOA_FEATURES", "region")
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path / "uploads"))
    canary = _pdf(tmp_path)
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app)
    bad = [str(canary), f"file://{canary}", f"data/../../{canary.name}", f"~/{canary.name}", f"upload/../{canary.name}",
           "upload/0123456789ab/../../" + canary.name, f"..\\{canary.name}", f"//{canary}"]
    body = {"page": 0, "space": "pdf_pt", "point": [150, 150], "points": [[100, 100], [300, 100], [300, 300]]}
    for route in ("snap", "scissors", "measure"):
        for p in bad:
            r = c.post(f"/feat/region/{route}", json={**body, "path": p})
            assert r.status_code in (400, 404), (route, p, r.status_code, r.text[:200])
    for route in ("suggest", "ask"):
        for p in bad:
            r = c.post(f"/feat/region/{route}", json={"path": p, "question": "What is room 101?", "region": {"bbox": [0, 0, 600, 400]}})
            assert r.status_code in (400, 404), (route, p, r.status_code, r.text[:200])


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


def test_area_questions_match_plurals():
    """H 23:52: 'areas' is an area question too ('Sum of all slab areas?', 'What are the areas in this box?')."""
    for q in ("Sum of all slab areas?", "What are the areas in this box?", "total floor areas", "What is the area of this outline?"):
        assert R._AREA_Q.search(q), q
    assert not R._AREA_Q.search("How many doors are in this area?".replace("area", "box"))


def test_length_snaps_onto_wall_faces_and_ends_on_pictures(tmp_path):
    """F1 cycle 4: the Length tool on a picture: segments along a wall move onto its face, the ends onto the walls the
    line runs into; a width across the room snaps its ends only; a line far from any wall stays as clicked."""
    p = _room_png(tmp_path)
    along = [[103, 347], [397, 348], [398, 103]]  # bottom + right walls, clicked 2-3 px inside: 10.78 m as clicked
    m = R.measure_shape(str(p), 0, "image_px", along, "length", snap=True, units_per_m=50)
    assert m["snapped"] and abs(m["length_m"] - 11.0) < 0.05, m  # (400-100) + (350-100) px = 11 m
    assert len(m["polyline"]) == 3 and abs(m["polyline"][1][0] - 400) < 1.5 and abs(m["polyline"][1][1] - 350) < 1.5
    raw = R.measure_shape(str(p), 0, "image_px", along, "length", snap=False, units_per_m=50)
    assert abs(raw["length_m"] - 10.78) < 0.02 and "polyline" not in raw
    width = R.measure_shape(str(p), 0, "image_px", [[104, 300], [396, 301]], "length", snap=True, units_per_m=50)
    assert abs(width["length_m"] - 6.0) < 0.05, width
    free = R.measure_shape(str(p), 0, "image_px", [[180, 180], [300, 260]], "length", snap=True, units_per_m=50)
    assert not free["snapped"] and abs(free["length_m"] - R._perimeter([[180, 180], [300, 260]], closed=False) / 50) < 1e-3


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


# ------------------------------------------------------------------------------------- measure tool engine (captain 21:42)

def _rect_pdf(tmp_path):
    """A 600 x 400 pt page at 1:100 (1 pt = 0.03528 m): a filled wall frame whose inner faces are x 100-300, y 100-250."""
    p = tmp_path / "rect.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=600, height=400)
    pg.insert_text((400, 380), "SCALE 1:100", fontsize=8)
    for r in ((96, 96, 304, 100), (96, 250, 304, 254), (96, 100, 100, 250), (300, 100, 304, 250)):
        pg.draw_rect(pymupdf.Rect(*r), color=(0, 0, 0), fill=(0, 0, 0), width=0)
    d.save(p)
    return p


def test_snap_point_vector_corner_and_none(tmp_path):
    p = str(_rect_pdf(tmp_path))
    s = R.snap_point(p, 0, "pdf_pt", [101.2, 98.9], 3.0)
    assert s["kind"] in ("corner", "intersection") and s["point"] == pytest.approx([100.0, 100.0], abs=0.01)
    far = R.snap_point(p, 0, "pdf_pt", [200, 180], 3.0)  # middle of the room: nothing to snap to
    assert far["kind"] == "none" and far["point"] == [200.0, 180.0]


def test_measure_shape_area_length_count(tmp_path):
    p = str(_rect_pdf(tmp_path))
    m_pt = 100 * 0.0254 / 72
    m = R.measure_shape(p, 0, "pdf_pt", [[100, 100], [300, 100], [300, 250], [100, 250]], "area")
    assert m["area_m2"] == pytest.approx(200 * 150 * m_pt ** 2, rel=1e-3)
    assert m["perimeter_m"] == pytest.approx(700 * m_pt, rel=1e-3) and m["scale"]["label"] == "1:100"
    assert "Area of your outline" in m["text"] and "from the drawing" in m["text"]
    L = R.measure_shape(p, 0, "pdf_pt", [[100, 100], [300, 100], [300, 250]], "length")
    assert L["length_m"] == pytest.approx(350 * m_pt, rel=1e-3)
    mine = R.measure_shape(p, 0, "pdf_pt", [[100, 100], [300, 100]], "length", units_per_m=20.0)
    assert mine["length_m"] == pytest.approx(10.0) and mine["scale"]["source"] == "set by you"
    assert R.measure_shape(p, 0, "pdf_pt", [[1, 1], [2, 2], [3, 3]], "count")["count"] == 3


def test_measure_router_reads_uploads(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    up = tmp_path / "uploads" / "abc123abc123"
    up.mkdir(parents=True)
    (up / "plan.pdf").write_bytes(_rect_pdf(tmp_path).read_bytes())
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path / "uploads"))
    app = FastAPI()
    app.include_router(R._router())
    c = TestClient(app)
    r = c.post("/feat/region/measure", json={"path": "upload/abc123abc123/plan.pdf", "points": [[100, 100], [300, 100], [300, 250], [100, 250]]})
    assert r.status_code == 200 and r.json()["area_m2"] > 3.7
    s = c.post("/feat/region/snap", json={"path": "upload/abc123abc123/plan.pdf", "point": [101, 99], "radius": 3})
    assert s.status_code == 200 and s.json()["kind"] != "none"
    assert c.post("/feat/region/measure", json={"path": "upload/../x.pdf", "points": []}).status_code == 400


def test_upload_abs_styled_picture(tmp_path, monkeypatch):
    """F8 22:42: a picture whose style the upload guessed is 'upload/<id>/<style>/<name>' in the browser but stored at
    <uploads>/<id>/data/<style>/<name>; the measure tool's resolver must find both forms and nothing outside."""
    from stoa.features.region import upload_abs

    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    (tmp_path / "0123456789ab" / "data" / "cubicasa5k").mkdir(parents=True)
    (tmp_path / "0123456789ab" / "data" / "cubicasa5k" / "plan.png").write_bytes(b"x")
    (tmp_path / "0123456789ab" / "sheet.pdf").write_bytes(b"x")
    assert upload_abs("upload/0123456789ab/cubicasa5k/plan.png") == str((tmp_path / "0123456789ab/data/cubicasa5k/plan.png").resolve())
    assert upload_abs("upload/0123456789ab/sheet.pdf") == str((tmp_path / "0123456789ab/sheet.pdf").resolve())
    assert upload_abs("upload/0123456789ab/missing.pdf") is None
    assert upload_abs("upload/0123456789ab/../x.pdf") is None
    assert upload_abs("upload/notanid/sheet.pdf") is None


def test_scissors_path_hugs_the_wall(tmp_path):
    """Magnetic lasso (captain 22:56): between two clicks inside a room's corners the path follows the wall's inner edge,
    not the straight chord (here an L-shaped room: the straight line from corner to corner cuts across the notch)."""
    import numpy as np
    from PIL import Image

    from stoa.features.region import scissors_path

    img = np.full((400, 400), 255, np.uint8)
    img[50:58, 50:350] = 0     # top wall
    img[50:350, 50:58] = 0     # left wall
    img[342:350, 50:200] = 0   # bottom wall (left part)
    img[200:350, 192:200] = 0  # notch: vertical
    img[200:208, 192:350] = 0  # notch: horizontal
    img[50:208, 342:350] = 0   # right wall (upper part)
    p = tmp_path / "room.png"
    Image.fromarray(img).save(p)
    r = scissors_path(str(p), 0, "image_px", [190, 340], [340, 199])  # bottom-right inner corner -> right inner corner
    assert r["kind"] == "magnetic"
    xs = [x for x, _ in r["path"]]
    ys = [y for _, y in r["path"]]
    # it goes up along the notch's inner face (x ~ 191) and then right along it (y ~ 199): it passes near (191, 199)
    assert any(abs(x - 191) <= 3 and abs(y - 199) <= 3 for x, y in r["path"])
    assert max(xs) <= 342 and max(ys) <= 342


def test_one_room_asked_takes_the_room_filling_the_box(tmp_path, monkeypatch):
    """UX M23: 'the room in here' with two labels in the box = the room that fills it (not the sum), no px in the card."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    monkeypatch.setattr(R, "ask_by_tags", lambda *a, **k: None)
    monkeypatch.setattr(R, "room_labels", lambda *a, **k: [{"text": "Hall", "box": [180, 180, 220, 190]},
                                                           {"text": "Store", "box": [262, 272, 290, 280]}])

    def solve(path, q):
        if "Hall" in q:
            return Answer(value=200.0, unit="m2", confidence=0.5, solver="pipeline/plan_rooms",
                          evidence=[Evidence(file=path, page=0, bbox=[105, 105, 255, 295], bbox_space="pdf_pt",
                                             note="What is the area of the Hall in this drawing?")])
        if "Store" in q:
            return Answer(value=5.0, unit="m2", confidence=0.5, solver="pipeline/plan_rooms",
                          evidence=[Evidence(file=path, page=0, bbox=[258, 262, 298, 298], bbox_space="pdf_pt")])
        return Answer(value=None, confidence=0.0)

    reg = {"page": 0, "bbox": [100, 100, 300, 300]}
    a = R.with_region(str(p), "What is the area of the room in this area?", reg, solve)
    assert a.value == 200.0 and a.assumptions[0].startswith("2 labelled rooms in your box")
    assert any("Store 5" in m for m in a.missing)
    assert not any(" px" in s or " pt" in s for s in a.assumptions)
    assert a.evidence[1].note == "Hall: 200 m²"  # the sub-solver's own question is not shown as evidence
    t = R.with_region(str(p), "What is the total area of the rooms in this area?", reg, solve)
    assert t.value == 205.0 and t.assumptions[0].startswith("2 rooms in your box: Hall 200 + Store 5")


def test_square_short_sides_closes_a_door_swing_bite_but_keeps_a_jog():
    """W5's traced selection on the DfE scan laundry: a flood fill cut the corner at the door swing (8 corners, sides
    < 0.6 m) -> the corner where the long sides meet; an L-shaped room's 0.4 m step (parallel neighbours) stays."""
    bite = [[2346.8, 2609.9], [2346.8, 2641.1], [2362.4, 2648.9], [2362.4, 2660.6], [2372.1, 2676.2], [2389.7, 2682.1],
            [2477.4, 2682.1], [2477.4, 2608]]
    assert R.square_short_sides(bite, 39.07) == [[2346.8, 2609.9], [2346.8, 2682.1], [2477.4, 2682.1], [2477.4, 2608.0]]
    jog = [[0, 0], [200, 0], [200, 100], [100, 100], [100, 116], [0, 116]]
    assert R.square_short_sides(jog, 39.07) is None
    assert R.square_short_sides([[0, 0], [100, 0], [100, 100], [0, 100]], 39.07) is None  # nothing short


def test_raster_bias_under_the_visitors_scale_uses_the_drawings_dpi(monkeypatch):
    """QA 00:56: a calibration on a scan ('set by you', no dpi) lost the half-pen correction (7.03 -> 6.86 m²)."""
    reg = R.parse_region({"page": 0, "space": "image_px", "bbox": [0, 0, 100, 100]})
    monkeypatch.setattr(R, "_outline_scale", lambda *a, **k: (0.0254, "printed 1:200 on A1 at 200 dpi: 1 m = 39.37 px"))
    want = R.RASTER_PEN_MM / 2 * 200 / 25.4
    assert abs(R._raster_bias_for("x.png", reg, "set by you") - want) < 1e-9
    assert abs(R._raster_bias_for("x.png", reg, "printed 1:200 on A1 at 200 dpi") - want) < 1e-9
    assert R._raster_bias_for("x.pdf", reg, "set by you") == 0.0


def _node22() -> str | None:
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.(\d+)", v)
    return node if m and (int(m[1]), int(m[2])) >= (22, 18) else None


@pytest.mark.skipif(_node22() is None, reason="needs Node >= 22.18 (type stripping)")
def test_plain_scale_node_tests():
    """A picture's scale in customer text is a ratio or where it came from, never '1 m = N px' (UX 02:35, SO-11)."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    test = root / "web" / "src" / "features" / "region" / "plainscale.test.ts"
    r = subprocess.run([_node22(), "--test", str(test)], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ℹ fail 0" in r.stdout


def _two_room_pdf(tmp_path):
    """'Kitchen' (x 120-280) and 'Pantry' (x 320-380) inside the box x 100-400, 'Bedroom' outside."""
    p = tmp_path / "two.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=600, height=400)
    pg.insert_text((150, 200), "Kitchen", fontsize=10)
    pg.insert_text((330, 200), "Pantry", fontsize=10)
    pg.insert_text((480, 200), "Bedroom", fontsize=10)
    d.save(p)
    return p


def _rooms_solve(path, q):
    rooms = {"Kitchen": ([120, 120, 280, 280], 12.5, 14.2), "Pantry": ([320, 150, 380, 250], 3.0, 7.1),
             "Bedroom": ([450, 120, 590, 280], 15.0, 16.0)}
    for name, (b, area, per) in rooms.items():
        if name in q:
            per_q = "perimeter" in q or "skirting" in q
            return Answer(value=per if per_q else area, unit="m" if per_q else "m2", confidence=0.55,
                          solver="pipeline/plan_rooms", evidence=[Evidence(
                              file=path, page=0, bbox=b, bbox_space="pdf_pt",
                              polygon=[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]])])
    return Answer(value=None, confidence=0.0)


def test_in_this_area_is_not_an_area_question():
    """'area' in 'in this area' is the region: 'Which rooms are in this area?' summed the rooms' m² (DfE box: 74.39)."""
    assert not R._wants_rooms_area("Which rooms are in this area?")
    assert R._wants_rooms_area("What is the total area of the rooms in this area?")
    assert R._wants_rooms_area("What is the area of the room in this area?")
    assert R._rooms_mode("Which rooms are in this area?") == "list"
    assert R._rooms_mode("What is the largest room in here?") == "max"
    assert R._rooms_mode("What is the perimeter of this room?") == "perimeter"
    assert R._rooms_mode("How long is the skirting in here?") == "skirting"
    for q in ("How many doors are in this area?", "How wide is the largest window?", "What is the area of this outline?",
              "Baseboard length of this outline (door openings excluded)?", "How many rooms are in this area?"):
        assert R._rooms_mode(q) == "", q


def test_room_list_superlative_and_perimeter_in_a_box(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "region")
    monkeypatch.setattr(R, "ask_by_tags", lambda *a, **k: None)
    p = _two_room_pdf(tmp_path)
    box = {"page": 0, "bbox": [100, 100, 400, 300]}
    calls = []

    def solve(path, q):
        calls.append(q)
        return _rooms_solve(path, q)

    a = R.with_region(str(p), "Which rooms are in this area?", box, solve)
    assert a.value == "Kitchen, Pantry" and a.solver == "region/room_labels" and not calls  # names only: no reader
    a = R.with_region(str(p), "List the rooms in here with their areas", box, solve)
    assert a.value == "Kitchen 12.5 m²; Pantry 3 m²" and a.unit is None
    a = R.with_region(str(p), "What is the largest room in this area?", box, solve)
    assert a.value == "Kitchen" and a.unit is None and "Kitchen 12.5" in a.assumptions[0]
    a = R.with_region(str(p), "What is the area of the smallest room in here?", box, solve)
    assert a.value == 3.0 and a.unit == "m2"
    calls.clear()
    a = R.with_region(str(p), "What is the perimeter of the rooms in this area?", box, solve)
    assert a.value == 21.3 and a.unit == "m" and all("perimeter of the" in q for q in calls)
    a = R.with_region(str(p), "How much skirting for this room?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert a.value == 14.2 and a.unit == "m" and a.solver == "region/labels"
    # 'the perimeter of the largest room' = the largest room by area (Kitchen), then its perimeter (was its name)
    a = R.with_region(str(p), "What is the perimeter of the largest room in this area?", box, solve)
    assert a.value == 14.2 and a.unit == "m" and "largest" in a.assumptions[0] and "Kitchen" in a.assumptions[0]
    a = R.with_region(str(p), "How much skirting does the smallest room in here need?", box, solve)
    assert a.value == 7.1 and a.unit == "m"
    # the unit the visitor names: the room is asked by name in the sheet's m² (DfE laundry: 7.0 m² for 'square feet')
    a = R.with_region(str(p), "How many square feet is this room?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert a.value == 134.55 and a.unit == "ft2" and any("converted" in s for s in a.assumptions)
    a = R.with_region(str(p), "How big is this room?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert a.value == 12.5 and a.unit == "m2"


def test_what_room_is_this_reads_the_printed_name(tmp_path, monkeypatch):
    """'What room is this?' in a box = the room name printed nearest its middle, no reader (was the masked copy, 5-9 s)."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _two_room_pdf(tmp_path)
    calls = []
    a = R.with_region(str(p), "What room is this?", {"page": 0, "bbox": [100, 100, 300, 300]},
                      lambda path, q: calls.append(q) or _rooms_solve(path, q))
    assert a.value == "Kitchen" and a.solver == "region/room_labels" and not calls and a.confidence >= 0.7
    a = R.with_region(str(p), "What is this room called?", {"page": 0, "bbox": [100, 100, 400, 300]}, _rooms_solve)
    assert a.value in ("Kitchen", "Pantry") and any("also named" in m for m in a.missing)


def test_two_counts_in_a_box_each_from_the_whole_sheets_marks(tmp_path, monkeypatch):
    """'How many doors and windows are in this area?' = each part's marks inside the box, joined 'a ; b' (asked as one
    question the masked copy said '14 ; 7' where the single asks say 9 / 6)."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)
    calls = []

    def solve(path, q):
        calls.append((path, q))
        if "doors" in q and "windows" not in q:
            xs = [140, 160, 500]
        elif "windows" in q and "doors" not in q:
            xs = [200, 520, 540]
        else:
            return Answer(value="14 ; 7", confidence=0.5, solver="pipeline/openings")
        ev = [Evidence(file=path, page=0, bbox=[x, 140, x + 10, 150], bbox_space="pdf_pt", note=f"item {i}")
              for i, x in enumerate(xs)]
        return Answer(value=len(xs), unit="count", confidence=0.7, evidence=ev, solver="pipeline/openings")

    a = R.with_region(str(p), "How many doors and windows are in this area?", {"page": 0, "bbox": [100, 100, 300, 300]},
                      solve)
    assert a.value == "2 ; 1" and a.solver.startswith("region/marks")
    assert all(path == str(p) for path, _ in calls) and {q for _, q in calls} == {
        "How many doors are on this plan?", "How many windows are on this plan?"}
    assert any(s.startswith("doors: ") for s in a.assumptions) and any(s.startswith("windows: ") for s in a.assumptions)


def test_wall_base_is_skirting_and_linear_feet_is_not_a_count(tmp_path):
    """F7 02:53: 'How many linear feet of wall base …' (AFCEC golds) got the outline's AREA; 'how many linear feet' is a
    length, not a count (the count paths would take a whole-sheet number as items)."""
    q = "How many linear feet of wall base do we need for this room?"
    assert R._BASEBOARD_Q.search(q) and not R._COUNT_Q.search(q) and R._rooms_mode(q) == "skirting"
    assert R._COUNT_Q.search("How many doors are in this area?") and not R._COUNT_Q.search("How many square metres is it?")
    assert R._rooms_mode("How many walls are in here?") == ""
    p = _room_png(tmp_path)
    reg = R.parse_region({"space": "image_px", "polygon": [[104, 96], [396, 104], [404, 346], [96, 354]]})
    a = R.outline_measure(str(p), q, reg, user_px_per_m=50)
    assert a.unit == "m" and abs(a.value - 21.0) / 21.0 < 0.02 and any("(no skirting)" in (e.note or "") for e in a.evidence)


def test_room_dims_from_its_own_outline(tmp_path, monkeypatch):
    """'How wide is this room?' / 'dimensions' in a box = the sides of the room's own outline at the printed scale (DfE
    laundry: the masked raster read said 'Cannot be determined'); an L-shaped room is said not to be a rectangle."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _pdf(tmp_path)  # 'Kitchen' at (150, 200), SCALE 1:100: 1 pt = 0.0352778 m
    m_pt = 0.0254 / 72 * 100
    shape = {"poly": [[120, 160], [280, 160], [280, 240], [120, 240]]}  # 160 x 80 pt = 5.64 x 2.82 m

    def solve(path, q):
        if "Kitchen" not in q:
            return Answer(value=None, confidence=0.0)
        poly = shape["poly"]
        area = R._poly_area(poly) * m_pt ** 2
        xs, ys = [x for x, _ in poly], [y for _, y in poly]
        return Answer(value=round(area, 2), unit="m2", confidence=0.77, solver="pipeline/plan_rooms",
                      evidence=[Evidence(file=path, page=0, bbox=[min(xs), min(ys), max(xs), max(ys)],
                                         bbox_space="pdf_pt", polygon=poly, note="Kitchen: area")])

    box = {"page": 0, "bbox": [100, 100, 300, 300]}
    assert R._rooms_mode("How wide is this room?") == "dims"
    assert R._rooms_mode("How long is this wall?") == "" and R._rooms_mode("How long is it here?") == ""
    a = R.with_region(str(p), "How wide is this room?", box, solve)
    assert a.solver == "region/room_dims" and a.unit == "m" and a.value == pytest.approx(80 * m_pt, abs=0.01)
    assert "Kitchen" in a.assumptions[0] and "a rectangle" in a.assumptions[0] and a.confidence >= 0.69
    a = R.with_region(str(p), "What is the length of this room?", box, solve)
    assert a.value == pytest.approx(160 * m_pt, abs=0.01)
    a = R.with_region(str(p), "What are the dimensions of this room?", box, solve)
    assert a.value == "5.64 m × 2.82 m" and a.unit is None and any(e.polygon for e in a.evidence)
    a = R.with_region(str(p), "How wide is this room in feet?", box, solve)
    assert a.unit == "ft" and a.value == pytest.approx(80 * m_pt / 0.3048, abs=0.01)
    shape["poly"] = [[120, 120], [280, 120], [280, 200], [200, 200], [200, 280], [120, 280]]  # an L
    a = R.with_region(str(p), "What are the dimensions of this room?", box, solve)
    assert "not a rectangle" in a.assumptions[0] and a.confidence < 0.5


def test_count_in_room_asks_the_room_by_name(tmp_path, monkeypatch):
    """'How many doors does this room have?' in a box = the room named in it asked by name on the full sheet (DfE
    laundry: pipeline/openings 1 @0.76 with its mark; the masked copy's raster read said 4 in 62 s)."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _two_room_pdf(tmp_path)
    where = {"x": 130}
    calls = []

    def solve(path, q):
        calls.append(q)
        if q == "How many doors does the Kitchen have?":
            ev = [Evidence(file=path, page=0, bbox=[where["x"], 130, where["x"] + 10, 140], bbox_space="pdf_pt",
                           note="door 1"),
                  Evidence(file=path, page=0, bbox=[where["x"] + 140, 200, where["x"] + 150, 210], bbox_space="pdf_pt",
                           note="door 2")]
            return Answer(value=2, unit="count", confidence=0.756, solver="pipeline/openings", evidence=ev)
        return Answer(value=None, confidence=0.0, solver="pipeline/prior")

    box = {"page": 0, "bbox": [100, 100, 300, 300]}
    a = R.with_region(str(p), "How many doors does this room have?", box, solve)
    assert a.value == 2 and a.solver == "region/room:pipeline/openings" and calls == ["How many doors does the Kitchen have?"]
    assert "Kitchen" in a.assumptions[0] and a.confidence == 0.75 and len(a.evidence) == 3  # + the drawn box
    where["x"] = 900  # its marks are elsewhere: another Kitchen; the other strategies answer
    a = R.with_region(str(p), "How many doors are in this room?", box, solve)
    assert not str(a.solver or "").startswith("region/room:")
    calls.clear()
    R.with_region(str(p), "How many people fit in this room?", box, solve)
    assert "How many people fit does the Kitchen have?" not in calls and not any("does the Kitchen" in q for q in calls)


def test_count_in_room_skips_a_name_printed_twice(tmp_path, monkeypatch):
    """Two 'Store' rooms on the sheet: 'How many doors does the Store have?' is ambiguous, so it is never asked (DfE 5-room
    box: vlm_lora 2 @0.20 with no marks, 19 s); the box's own marks answer, said so."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = tmp_path / "stores.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=600, height=400)
    pg.insert_text((150, 200), "Store", fontsize=10)
    pg.insert_text((480, 200), "Store", fontsize=10)
    d.save(p)
    calls = []

    def solve(path, q):
        calls.append(q)
        if q == "How many doors are on this plan?":
            ev = [Evidence(file=path, page=0, bbox=[x, 140, x + 10, 150], bbox_space="pdf_pt", note=f"door {i}")
                  for i, x in enumerate([150, 200, 500])]
            return Answer(value=3, unit="count", confidence=0.756, solver="pipeline/openings", evidence=ev)
        return Answer(value=None, confidence=0.0, solver="pipeline/prior")

    a = R.with_region(str(p), "How many doors does this room have?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert not any("the Store" in q for q in calls)
    assert a.value == 2 and a.solver.startswith("region/marks:") and "no single named room" in a.assumptions[0]
    assert a.confidence <= 0.5


def test_count_in_room_zero_by_name_loses_to_marks_in_the_box(tmp_path, monkeypatch):
    """DfE PNG laundry: 'How many doors does the Laundry have?' = room_graph 0 @0.65, but the whole sheet's door marks put
    1 inside the box (the PDF's answer): the marks are counted and both readings are said."""
    monkeypatch.setenv("STOA_FEATURES", "region")
    p = _two_room_pdf(tmp_path)

    def solve(path, q):
        if q == "How many doors does the Kitchen have?":
            return Answer(value=0, unit="count", confidence=0.65, solver="pipeline/room_graph",
                          evidence=[Evidence(file=path, page=0, bbox=[120, 120, 280, 280], bbox_space="pdf_pt")])
        if q == "How many doors are on this plan?":
            ev = [Evidence(file=path, page=0, bbox=[x, 140, x + 10, 150], bbox_space="pdf_pt", note=f"door {i}")
                  for i, x in enumerate([150, 500])]
            return Answer(value=2, unit="count", confidence=0.756, solver="pipeline/openings", evidence=ev)
        return Answer(value=None, confidence=0.0, solver="pipeline/prior")

    a = R.with_region(str(p), "How many doors does this room have?", {"page": 0, "bbox": [100, 100, 300, 300]}, solve)
    assert a.value == 1 and a.solver.startswith("region/marks:") and "said 0" in a.assumptions[0] and a.confidence <= 0.5
