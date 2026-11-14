"""F5 revision compare: synthetic sheets with known edits (no dataset needed)."""

from __future__ import annotations

import math

import numpy as np
import pytest

fitz = pytest.importorskip("fitz")
cv2 = pytest.importorskip("cv2")

from stoa.features import revcompare as rc  # noqa: E402

TITLE = ["Education", "Funding", "Agency", "Ground", "Floor", "Plan", "Scale", "1:100", "Drawing", "No.", "A-201",
         "Revision", "Checked", "Project", "Northside", "School"]


def plan(path, *, kitchen="Kitchen", kitchen_area="A: 12 m2", store_area="A: 8 m2", extra_wall=False,
         drop_door=False, stair="Stair", wall_x=300.0) -> None:
    """A small floor plan: 3 rooms, walls, door swings, room labels with printed areas and a title block."""
    doc = fitz.open()
    p = doc.new_page(width=842, height=595)
    sh = p.new_shape()
    sh.draw_rect(fitz.Rect(80, 80, 680, 460))
    sh.draw_line((wall_x, 80), (wall_x, 300))
    sh.draw_line((300, 300), (300, 460))
    sh.draw_line((500, 80), (500, 460))
    sh.draw_line((80, 300), (300, 300))
    sh.finish(color=(0, 0, 0), width=3)
    for x, y in ((300, 200), (500, 380)) + (() if drop_door else ((190, 300),)):  # door swings (quarter circles)
        sh.draw_line((x, y), (x + 40, y))
        sh.draw_sector((x, y), (x + 40, y), -90)
        sh.finish(color=(0, 0, 0), width=0.8, closePath=False)  # one door per path: dropping one changes no other
    if extra_wall:
        sh.draw_line((500, 230), (680, 230))
        sh.finish(color=(0, 0, 0), width=3)
    sh.commit()
    for (x, y), name, area in (((150, 180), kitchen, kitchen_area), ((380, 250), "Store", store_area),
                               ((560, 150), "Office", "A: 20 m2"), ((150, 380), stair, "A: 9 m2")):
        p.insert_text((x, y), name, fontsize=10)
        p.insert_text((x, y + 13), area, fontsize=8)
    x = 90
    for i, w in enumerate(TITLE):
        p.insert_text((x, 520 + 14 * (i % 3)), w, fontsize=9)
        x += 45 if i % 3 == 2 else 0
    doc.save(path)


def to_scan(pdf, png, angle=1.5, scale=0.96, dpi=150, seed=0) -> None:
    with fitz.open(pdf) as d:
        pix = d[0].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()
    h, w = g.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
    M[:, 2] += (25, -10)
    g = cv2.warpAffine(g, M, (w + 40, h + 40), borderValue=240)
    g = cv2.GaussianBlur(g, (0, 0), 0.8)
    g = np.clip(g.astype(np.float32) * 0.93 + 10 + np.random.default_rng(seed).normal(0, 5, g.shape), 0, 255)
    cv2.imwrite(str(png), g.astype(np.uint8))


def overlaps(box, target, grow=6.0) -> bool:
    return box[0] < target[2] + grow and box[2] > target[0] - grow and box[1] < target[3] + grow and \
        box[3] > target[1] - grow


@pytest.fixture()
def sheets(tmp_path):
    a, b = tmp_path / "rev_a.pdf", tmp_path / "rev_b.pdf"
    plan(a)
    plan(b, kitchen="Pantry", kitchen_area="A: 10 m2", extra_wall=True, drop_door=True)
    return a, b


def test_identical_sheets_have_no_changes(sheets):
    a, _ = sheets
    res = rc.compare(str(a), str(a))
    assert res["alignment"]["ok"] and res["alignment"]["method"] == "text"
    assert res["regions"] == [] and res["text"] == []
    assert res["summary"].startswith("No differences found")


def test_vector_pair_finds_each_edit(sheets):
    a, b = sheets
    res = rc.compare(str(a), str(b))
    al = res["alignment"]
    assert al["ok"] and al["method"] == "text" and al["residual"] < 0.5 and abs(al["scale"] - 1) < 1e-3
    boxes = [r["box_a"] for r in res["regions"]]
    assert any(overlaps(bx, (500, 228, 680, 232)) for bx in boxes), "new wall not found"
    assert any(overlaps(bx, (190, 300, 232, 342)) for bx in boxes), "removed door not found"
    assert all(overlaps(bx, (500, 228, 680, 232)) or overlaps(bx, (190, 300, 232, 342)) for bx in boxes), boxes
    changed = {(t["old"], t["new"]) for t in res["text"] if t["kind"] == "changed"}
    assert ("Kitchen", "Pantry") in changed and ("A: 12 m2", "A: 10 m2") in changed
    q = [x for x in res["quantities"] if x["unit"] == "m2"]
    assert q and q[0]["old"] == 12 and q[0]["new"] == 10 and q[0]["delta"] == -2
    assert res["same_sheet"] and not res["approximate"]
    assert any("door" in s["question"] for s in res["suggested_questions"])
    d = res["doors"]  # the openings reader's swing arcs on both sheets: the dropped door is the one removed swing
    assert d and d["count_old"] - d["count_new"] == 1 and len(d["removed"]) == 1 and not d["added"]
    assert overlaps(d["removed"][0]["box_a"], (190, 300, 232, 342))


def test_printed_room_areas_compared_by_name(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    plan(a)
    plan(b, store_area="A: 6 m2")
    res = rc.compare(str(a), str(b))
    by = {d["name"]: d for d in res["areas"]["by_name"]}
    assert set(by) == {"Store"} and by["Store"]["old"] == [8.0] and by["Store"]["new"] == [6.0]
    assert res["areas"]["total"]["delta"] == -2.0
    assert "Store: 8 → 6 m²" in res["summary"]
    assert res["suggested_questions"][0]["question"] == "What is the area of the Store?"


def test_scanned_revision_aligns_and_marks_the_new_wall(sheets, tmp_path):
    a, b = sheets
    png = tmp_path / "rev_b_scan.png"
    to_scan(b, png)
    res = rc.compare(str(a), str(png))
    al = res["alignment"]
    assert al["ok"] and al["method"].startswith("features"), al
    assert abs(al["rotation_deg"] + 1.5) < 0.3 or abs(al["rotation_deg"] - 1.5) < 0.3
    assert res["approximate"] and res["b"]["space"] == "image_px"
    boxes = [r["box_a"] for r in res["regions"]]
    assert any(overlaps(bx, (500, 228, 680, 232), grow=10) for bx in boxes), boxes
    # B's boxes are in the scan's pixels: the new wall maps inside the image
    wall = next(r for r in res["regions"] if overlaps(r["box_a"], (500, 228, 680, 232), grow=10))
    W, H = res["b"]["size"]
    assert 0 <= wall["box_b"][0] < W and 0 <= wall["box_b"][1] < H


def test_unrelated_sheets_are_not_marked(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    plan(a)
    doc = fitz.open()
    p = doc.new_page(width=842, height=595)
    rng = np.random.default_rng(1)
    for _ in range(60):
        x, y = rng.uniform(50, 790), rng.uniform(50, 545)
        p.draw_circle((x, y), rng.uniform(5, 30), width=2)
    for i, w in enumerate(TITLE[:4]):
        p.insert_text((600, 100 + 30 * i), w, fontsize=9)
    doc.save(b)
    res = rc.compare(str(a), str(b))
    assert not res["alignment"]["ok"] or not res["same_sheet"]
    if not res["alignment"]["ok"]:
        assert res["regions"] == [] and "Could not line the two sheets up" in res["summary"]


def test_router_is_off_without_the_flag_and_serves_uploads(sheets, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    a, b = sheets
    up = tmp_path / "uploads"
    (up / "x1").mkdir(parents=True)
    (up / "x2").mkdir()
    (up / "x1" / "a.pdf").write_bytes(a.read_bytes())
    (up / "x2" / "b.pdf").write_bytes(b.read_bytes())
    monkeypatch.setattr(rc, "UPLOADS", up)
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    app = fastapi.FastAPI()
    app.include_router(rc.router)
    c = TestClient(app)
    body = {"a": "upload/x1/a.pdf", "b": "upload/x2/b.pdf"}
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    assert c.post("/feat/revcompare/compare", json=body).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "other,revcompare")
    r = c.post("/feat/revcompare/compare", json=body)
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["a"]["file"] == "upload/x1/a.pdf" and js["regions"] and js["cost"]["tier"] == "cheap"
    img = c.get(js["images"]["overlay"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert c.post("/feat/revcompare/compare", json={**body, "b": "upload/../etc/passwd"}).status_code == 400
    assert c.post("/feat/revcompare/compare", json={**body, "b": "/etc/passwd"}).status_code == 400
    assert c.post("/feat/revcompare/compare", json=body).json()["cached"] is True


def test_moved_wall_is_reported_in_metres_at_the_printed_scale(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    plan(a)
    plan(b, wall_x=312.0)  # the upper half of the left partition moves 12 pt = 0.42 m at the printed 1:100
    res = rc.compare(str(a), str(b))
    assert res["scale"] and res["scale"]["ratio"] == "1:100"
    moved = [r for r in res["regions"] if r.get("moved_m")]
    assert len(moved) == 1 and abs(moved[0]["moved_m"] - 0.42) < 0.06, res["regions"]
    assert "right on the sheet" in moved[0]["description"]


def test_set_page_matching_the_sheet_on_screen(tmp_path, sheets):
    a, b = sheets
    other = tmp_path / "other.pdf"
    doc = fitz.open()
    pg = doc.new_page(width=842, height=595)
    for i, w in enumerate(["Roof", "Plan", "Gutter", "Ridge", "Notes", "Section", "Detail", "Flashing"]):
        pg.insert_text((100, 100 + 20 * i), w, fontsize=10)
    doc.save(other)
    s = fitz.open()
    for f in (other, b, other):
        with fitz.open(f) as d:
            s.insert_pdf(d)
    s.save(tmp_path / "set.pdf")
    res = rc.compare(str(a), str(tmp_path / "set.pdf"), page_b=None)
    assert res["b"]["page"] == 1 and "page 2 matches" in res["page_b_note"]
    assert res["alignment"]["ok"] and res["alignment"]["method"] == "text"


# ------------------------------------------------------------------ cycle 2: labels on scans (OCR mocked)
@pytest.fixture(autouse=True)
def _no_real_ocr(monkeypatch):
    """Tests never call the shared OCR engine: a crop reading raises unless a test replaces it."""
    def boom(crops):
        raise RuntimeError("no OCR in tests")

    monkeypatch.setattr(rc, "ocr_crops", boom)


def test_renamed_label_on_a_scan_is_read_from_the_changed_crop_only(tmp_path, monkeypatch):
    a, b, png = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "b_scan.png"
    plan(a)
    plan(b, kitchen="Pantry")
    to_scan(b, png, dpi=200)
    seen = []

    def fake(crops):
        seen.append(len(crops))
        return [("Pantry", 0.95)] * len(crops), {"crops": len(crops), "cpu_seconds": 0.1}

    monkeypatch.setattr(rc, "ocr_crops", fake)
    from stoa import ocr

    monkeypatch.setattr(ocr, "available", lambda: True)
    res = rc.compare(str(a), str(png))
    assert res["alignment"]["ok"]
    rows = [t for t in res["text"] if t.get("source")]
    assert [(t["kind"], t["old"], t["new"]) for t in rows] == [("changed", "Kitchen", "Pantry")]
    assert seen and seen[0] <= 2  # only the label that differs is read (one crop of the scan)
    x0, y0, x1, y1 = rows[0]["box_a"]
    assert 140 <= x0 <= 160 and 165 <= y1 <= 185  # Kitchen's label at (150, 180)
    assert res["text_check"]["checked"] >= 8 and res["text_check"]["ocr"]["crops"] == seen[0]


def test_scan_text_check_fails_closed_without_ocr(tmp_path):
    a, b, png = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "b_scan.png"
    plan(a)
    plan(b, kitchen="Pantry")
    to_scan(b, png, dpi=200)
    res = rc.compare(str(a), str(png))  # the autouse fixture's OCR raises
    assert res["alignment"]["ok"] and not [t for t in res["text"] if t.get("source")]
    assert "could not be read" in (res["text_check"]["note"] or "")


def test_ocr_noise_is_not_a_change():
    assert rc._same_text("Kitchen", "Kitchon")
    assert rc._same_text("OFFICE", "0FFICE")
    assert rc._same_text("Store 1.02", "Store 1.02.")
    assert not rc._same_text("Yr 4", "Yr 5")
    assert not rc._same_text("Office", "Store")
    assert not rc._same_text("A: 62 m2", "A: 55 m2")


def test_ocr_words_snap_to_known_words_but_numbers_never_do():
    vocab = {w.lower(): w for w in rc.ROOM_WORDS}
    assert rc._snap("Starage", vocab) == "Storage"
    assert rc._snap("Yr 4", vocab) == "Yr 4"
    assert rc._snap("Room 15", vocab) == "Room 15"


def test_blur_gap_measures_how_much_blurrier_the_other_copy_is(tmp_path):
    pdf = tmp_path / "p.pdf"
    plan(pdf)
    a = rc.load_sheet(str(pdf))
    valid = np.ones(a.gray.shape, bool)
    assert rc._blur_gap(a, a.gray, valid) == 0.0
    assert rc._blur_gap(a, cv2.GaussianBlur(a.gray, (0, 0), 2.0), valid) in (1.5, 2.25)
    a2 = rc.Sheet(a.path, a.kind, a.space, cv2.GaussianBlur(a.gray, (0, 0), 2.0), a.zoom, a.native)
    assert rc._blur_gap(a2, a.gray, valid) in (-1.5, -2.25)  # the drawing on screen is the blurrier one


def test_demo_pair_is_served_for_the_one_click_example(sheets, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    a, b = sheets
    d = dict(rc.DEMOS["dfe630"])
    (tmp_path / d["a"]).write_bytes(a.read_bytes())
    (tmp_path / d["b"]).write_bytes(b.read_bytes())
    monkeypatch.setattr(rc, "DEMO_DIR", tmp_path)
    app = fastapi.FastAPI()
    app.include_router(rc.router)
    c = TestClient(app)
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    assert c.get("/feat/revcompare/demo").status_code == 404  # off without the flag
    monkeypatch.setenv("STOA_FEATURES", "revcompare")
    lst = c.get("/feat/revcompare/demo").json()["demos"]
    assert lst[0]["id"] == "dfe630" and lst[0]["available"] and "Open Government Licence" in lst[0]["credit"]
    r = c.get(lst[0]["b"]["url"])
    assert r.status_code == 200 and r.content == b.read_bytes() and r.headers["content-type"] == "application/pdf"
    assert c.get("/feat/revcompare/demo/dfe630/c").status_code == 404
    assert c.get("/feat/revcompare/demo/nope/a").status_code == 404
    monkeypatch.setattr(rc, "DEMO_DIR", tmp_path / "missing")
    assert c.get("/feat/revcompare/demo").json()["demos"][0]["available"] is False


def test_an_edit_inside_dense_hatching_is_found_and_noise_is_not():
    rng = np.random.default_rng(0)
    g = np.full((900, 900), 245, np.uint8)
    for y in range(100, 800, 7):  # siding: a line every 7 px (any changed pixel is within 3 px of a line)
        g[y:y + 2, 100:800] = 30
    noisy = lambda x: np.clip(x.astype(np.float32) + rng.normal(0, 4, x.shape), 0, 255).astype(np.uint8)
    edited = g.copy()
    edited[400:440, 400:520] = 245
    cv2.putText(edited, "STORE", (402, 432), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 30, 2)
    a = noisy(g)
    valid = np.ones(g.shape, bool)
    ink = lambda x: x < 140
    same = noisy(g)
    assert rc.dense_regions(ink(a), ink(same), valid, a, same, 5, []) == []
    b = noisy(edited)
    regs = rc.dense_regions(ink(a), ink(b), valid, a, b, 5, [])
    assert len(regs) == 1
    x0, y0, x1, y1 = regs[0]["box_px"]
    assert x0 < 520 and x1 > 400 and y0 < 440 and y1 > 400
    assert rc.dense_regions(ink(a), ink(b), valid, a, b, 5, [[380, 380, 540, 460]]) == []  # already marked: no repeat


def test_the_flag_accepts_all_like_the_registry(monkeypatch):
    for v, want in (("all", True), ("revcompare", True), ("x, revcompare ", True), ("", False), ("region", False)):
        monkeypatch.setenv("STOA_FEATURES", v)
        assert rc.enabled() is want, v


def other_sheet(path, *, title="First Floor Plan", rooms=("Library", "Music", "Art Room", "Hall", "Lobby", "Plant")):
    """A sheet of another floor: a different room grid, other room names, its own title line."""
    doc = fitz.open()
    p = doc.new_page(width=842, height=595)
    sh = p.new_shape()
    for i, nm in enumerate(rooms):
        x, y = 100 + 190 * (i % 3), 90 + 170 * (i // 3)
        sh.draw_rect(fitz.Rect(x, y, x + 170, y + 150))
        sh.draw_circle((x + 120, y + 100), 18)
        p.insert_text((x + 12, y + 30), nm, fontsize=10)
        p.insert_text((x + 12, y + 43), f"A: {30 + 7 * i} m2", fontsize=8)
    sh.finish(color=(0, 0, 0), width=2.5)
    sh.commit()
    p.insert_text((520, 560), title, fontsize=16)
    p.insert_text((720, 580), "A-202", fontsize=9)
    doc.save(path)


def merge(out, parts) -> None:
    d = fitz.open()
    for x in parts:
        with fitz.open(x) as s:
            d.insert_pdf(s)
    d.save(out)


def test_issue_sets_pair_sheets_by_their_words(tmp_path):
    """'What changed in this issue': every sheet of the old issue finds its sheet in the new one whatever the page
    order; a sheet only in the new issue is listed as new (titles and sheet numbers read from the page)."""
    gf, gf2, ff, site = (tmp_path / n for n in ("gf.pdf", "gf2.pdf", "ff.pdf", "site.pdf"))
    plan(gf)
    plan(gf2, kitchen_area="A: 10 m2", extra_wall=True)
    other_sheet(ff)
    other_sheet(site, title="Site Plan", rooms=("Car Park", "Bins", "Bike Store", "Pitch", "Garden", "Gate House"))
    merge(tmp_path / "A.pdf", [gf, ff])
    merge(tmp_path / "B.pdf", [ff, site, gf2])
    r = rc.match_sets([(str(tmp_path / "A.pdf"), "upload/x/A.pdf")], [(str(tmp_path / "B.pdf"), "upload/y/B.pdf")])
    assert {(p["a"], p["b"]) for p in r["pairs"]} == {(0, 2), (1, 0)}
    assert all(p["how"] == "words" for p in r["pairs"])
    assert r["only_b"] == [1] and r["only_a"] == []
    assert r["sheets_b"][1]["title"] == "Site Plan" and r["sheets_a"][1]["title"] == "First Floor Plan"
    assert r["sheets_a"][1]["sheet_no"] == "A-202" and r["sheets_a"][0]["file"] == "upload/x/A.pdf"
    assert "1 only in the new issue" in r["summary"]
    # each pair is then an ordinary compare of those two pages: the edited ground floor shows its change
    res = rc.compare(str(tmp_path / "A.pdf"), str(tmp_path / "B.pdf"), 0, 2)
    assert res["alignment"]["ok"] and any(t.get("new") == "A: 10 m2" for t in res["text"])


def test_issue_sets_pair_scans_by_their_shapes(tmp_path):
    """A new issue that arrives as scans (no text layer): sheets are paired by image features."""
    gf, gf2, ff = (tmp_path / n for n in ("gf.pdf", "gf2.pdf", "ff.pdf"))
    plan(gf)
    plan(gf2, extra_wall=True)
    other_sheet(ff)
    merge(tmp_path / "A.pdf", [gf, ff])
    to_scan(ff, tmp_path / "s1.png", seed=1)
    to_scan(gf2, tmp_path / "s2.png", seed=2)
    r = rc.match_sets([(str(tmp_path / "A.pdf"), "A.pdf")],
                      [(str(tmp_path / "s1.png"), "s1.png"), (str(tmp_path / "s2.png"), "s2.png")])
    assert {(p["a"], p["b"]) for p in r["pairs"]} == {(0, 1), (1, 0)}
    assert all(p["how"] == "features" for p in r["pairs"]) and not r["only_a"] and not r["only_b"]


def test_whole_issue_example_is_served_as_two_page_sets(sheets, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    a, b = sheets
    d = rc.DEMOS["dfe630set"]
    src = tmp_path / "src"
    src.mkdir()
    for name in {*d["a"], *d["b"]}:
        (src / name).write_bytes((b if "55m2" in name else a).read_bytes())
    monkeypatch.setattr(rc, "DEMO_DIR", src)
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("STOA_FEATURES", "revcompare")
    app = fastapi.FastAPI()
    app.include_router(rc.router)
    c = TestClient(app)
    got = {x["id"]: x for x in c.get("/feat/revcompare/demo").json()["demos"]}
    assert got["dfe630set"]["available"] and got["dfe630"]["available"]  # the set reuses the single pair's files
    r = c.get(got["dfe630set"]["b"]["url"])
    assert r.status_code == 200
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        assert len(doc) == 2
