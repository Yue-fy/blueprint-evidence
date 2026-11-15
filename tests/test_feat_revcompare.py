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
    assert js["a"]["name"]["file"] == "a.pdf" and js["b"]["name"]["title"]  # UX NEW-11: each version's printed name
    nm = c.post("/feat/revcompare/name", json={"path": "upload/x1/a.pdf"})  # UX C-7: the sheet on screen, uncompared
    assert nm.status_code == 200 and nm.json()["file"] == "a.pdf" and nm.json()["title"]
    assert c.post("/feat/revcompare/name", json={"path": "/etc/passwd"}).status_code == 400
    assert c.post("/feat/revcompare/name", json={"path": "upload/x1/none.pdf"}).status_code == 404


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


def _fake_layer(a, b, M, edits):
    """A's printed lines placed on scan B (its image px) as an OCR text layer would read them, with readings edited."""
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    Minv = np.linalg.inv(M)
    items = []
    for x0, y0, x1, y1, t in a.lines:
        q = cv2.perspectiveTransform(np.float32([[x0 * a.zoom, y0 * a.zoom], [x1 * a.zoom, y1 * a.zoom]])
                                     .reshape(-1, 1, 2), Minv).reshape(-1, 2) / b.zoom
        items.append(TextItem(edits.get(t, t), [float(q[:, 0].min()), float(q[:, 1].min()),
                                                float(q[:, 0].max()), float(q[:, 1].max())]))
    return TextLayer(str(b.path), (int(b.native[0]), int(b.native[1])), items)


@pytest.mark.parametrize("real_change", [False, True])
def test_room_areas_on_a_scan_come_from_its_ocr_layer(tmp_path, monkeypatch, real_change):
    """A scan has no text layer of its own: room tags come from the upload's OCR layer (names snapped to the CAD
    sheet's). An OCR misread ('12' read as '18') on an unchanged tag is set back by the pixels; a real change stays."""
    import stoa.solvers.raster.sheet as rsheet

    a_pdf, b_pdf, b_png = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "b.png"
    plan(a_pdf)
    plan(b_pdf, kitchen_area="A: 10 m2" if real_change else "A: 12 m2")
    to_scan(b_pdf, b_png, dpi=150)
    a, b = rc.load_sheet(str(a_pdf)), rc.load_sheet(str(b_png))
    M = rc.align(a, b).M
    edits = {"A: 12 m2": "A: 10 m2" if real_change else "A: 18 m2", "Kitchen": "Kitchon", "Office": "Ofice"}
    layer = _fake_layer(a, b, M, edits)
    monkeypatch.setattr(rsheet, "cached_layer", lambda p: layer if str(p) == str(b_png) else None)
    res = rc.compare(str(a_pdf), str(b_png))
    by = {d["name"]: d for d in res["areas"]["by_name"]}
    assert res["areas_pending"] is False
    if real_change:
        assert by["Kitchen"]["old"] == [12.0] and by["Kitchen"]["new"] == [10.0]
        assert "OCR" in res["areas"]["source"]
    else:
        assert by == {} and res["areas"].get("ocr_overruled") == 1  # 'Kitchon' / 'Ofice' snapped, '18' set back
    monkeypatch.setattr(rsheet, "cached_layer", lambda p: None)  # not read yet: a fresh upload says 'pending'
    assert rc.compare(str(a_pdf), str(b_png))["areas_pending"] is True


# ------------------------------------------------------------------ the cost of a revision (captain 21:51)
def test_door_pairs_count_a_double_door_as_one_set():
    r = 10.0
    double = [{"c": (0.0, 0.0), "r": r, "p0": (10.0, 0.0), "p1": (0.0, 10.0)},  # closed leaves meet in the middle
              {"c": (20.0, 0.0), "r": r, "p0": (10.0, 0.0), "p1": (20.0, 10.0)}]
    singles = [{"c": (100.0, 0.0), "r": r, "p0": (100.0, 10.0), "p1": (90.0, 0.0)},  # 2 r apart, leaves do not meet
               {"c": (120.0, 0.0), "r": r, "p0": (120.0, 10.0), "p1": (110.0, 0.0)}]
    assert rc._door_pairs(double + singles) == {0: 1, 1: 0}


def test_revision_cost_prices_the_quantity_changes(sheets):
    a, b = sheets  # Kitchen 12 m2 -> Pantry 10 m2, a new 180-pt wall, one door dropped
    c = rc.compare(str(a), str(b))["revision_cost"]
    lines = {ln["id"]: ln for ln in c["lines"]}
    assert lines["floor:Kitchen"]["delta"] == -12.0 and lines["floor:Pantry"]["delta"] == 10.0
    assert lines["floor:Kitchen"]["eur"] == -12.0 * 38.0 and lines["floor:Kitchen"]["marks"]
    assert lines["doors:removed"]["delta"] == -1 and lines["doors:removed"]["eur"] == -450.0
    walls = [ln for ln in c["lines"] if ln["id"].startswith("wall:")]
    assert len(walls) == 1 and 5.5 < walls[0]["delta"] < 6.6, walls  # 180 pt at 1:100 = 6.35 m
    assert walls[0]["eur"] == pytest.approx(walls[0]["delta"] * rc.WALL_H * 95.0, abs=0.01)
    assert c["total_eur"] == pytest.approx(sum(ln["eur"] for ln in c["lines"]), abs=0.05)
    assert c["skirting"] == "pending" and "example prices" in c["summary"]
    assert not c["review"]  # the dropped door's own linework is priced as a door, not listed as a wall to check


def test_area_tags_in_brackets_are_read():
    # DfE 2012 baseline designs print '(55.0 m2)' under the room name: the unseen 420-place pair priced no floor at all
    for t in ("(55.0 m2)", "[12 m²]", "A: 62 m2", "468 SF", "(6.0 m2"):
        assert rc._AREA_LINE.match(t), t
    for t in ("GF Area 1645m2", "(55.0 m2) x 2", "Yr 4"):
        assert not rc._AREA_LINE.match(t), t


def test_ocr_misreads_of_an_unchanged_scan_are_not_renames():
    # F5 noise pairs (16 Stoa sheets vs X1's simulated scans of the SAME sheets): 8 false 'renamed label' rows -> 0
    vocab = {w.lower(): w for w in rc.ROOM_WORDS} | {"above": "Above", "chair": "Chair", "inspection": "Inspection"}
    if rc._dictionary():  # the rule needs a system word list (off without one)
        for junk in ("ine acicn", "Abom", "Chetr", "('cturting"):
            assert not rc._wordlike(junk, vocab), junk
    for ok in ("Store", "Staff Room", "SWR", "Yr 4 Lockers", "Hygiene Rm", "KitcheneWALL", "Music 2"):
        assert rc._wordlike(ok, vocab), ok
    # the crop took in the room number printed beside the name: no rename
    assert rc._spillover("Music", "Music 2", {"music", "2"})
    assert rc._spillover("English 4", "English", {"english", "4"})
    assert not rc._spillover("Music", "Music 3", {"music", "2"})
    assert not rc._spillover("Office", "Store", {"office", "store"})
    assert not rc._spillover("Room 2", "2", {"2"})


def test_doors_on_a_scan_are_left_to_a_person_only_where_something_changed():
    base = {"a": {"kind": "vector"}, "b": {"kind": "scan"}, "approximate": True, "doors": None,
            "areas": {"rooms": [], "by_name": []}, "regions": []}
    assert rc.revision_cost(base)["review"] == []  # nothing changed on the scan: no door changed either
    reg = {"id": 1, "kind": "added", "box_a": [10, 10, 40, 30], "size_m": [1.0, 0.8], "near": None}
    rv = {v["id"]: v for v in rc.revision_cost({**base, "regions": [reg]})["review"]}
    assert rv["review:doors"]["marks"] == [{"box": [10, 10, 40, 30], "kind": "added"}]


def test_wet_rooms_are_tiled_and_unmeasured_changes_stay_out_of_the_total():
    res = {"a": {"kind": "vector"}, "b": {"kind": "vector"}, "approximate": False, "doors": None,
           "areas": {"rooms": [], "by_name": [{"name": "Pupil Toilets", "unit": "m2", "old": [8.0], "new": [6.0],
                                               "count_old": 1, "count_new": 1, "sum_old": 8.0, "sum_new": 6.0,
                                               "delta": -2.0}]},
           "regions": [{"id": 1, "kind": "changed", "box_a": [0, 0, 300, 300], "size_m": [10.6, 10.6], "near": None}]}
    c = rc.revision_cost(res)
    (ln,) = c["lines"]
    assert ln["price"] == "floor_wet" and ln["eur"] == -130.0 and c["total_eur"] == -130.0
    assert [v["id"] for v in c["review"]] == ["review:walls"] and c["skirting"] == "n/a"


def test_skirting_is_priced_only_where_the_traced_outline_matches_the_printed_area(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    plan(a, kitchen_area="A: 59 m2")  # the kitchen's printed area = its traced outline (7.7 x 7.7 m at 1:100)
    plan(b, kitchen_area="A: 64 m2", store_area="A: 6 m2", wall_x=320.0)  # wider kitchen; Store's tag is not its room
    res = rc.compare(str(a), str(b))
    sk = rc.skirting_changes(res, str(a), str(b))
    c = rc.revision_cost(res, sk)
    lines = {ln["id"]: ln for ln in c["lines"]}
    k = lines["skirting:Kitchen"]
    assert k["old"] > 20 and k["new"] > k["old"] and len(k["outlines"]) == 2 and c["skirting"] == "done"
    assert k["eur"] == pytest.approx((k["new"] - k["old"]) * 12.0, abs=0.01)
    assert "skirting:Store" not in lines
    store = [v for v in c["review"] if v["id"] == "review:skirting:Store"]
    assert store and "does not match the printed" in store[0]["why"]


def test_cost_route_adds_skirting_and_is_off_without_the_flag(sheets, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    a, b = sheets
    up = tmp_path / "uploads"
    for k, src in (("x1", a), ("x2", b)):
        (up / k).mkdir(parents=True)
        (up / k / src.name).write_bytes(src.read_bytes())
    monkeypatch.setattr(rc, "UPLOADS", up)
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    app = fastapi.FastAPI()
    app.include_router(rc.router)
    c = TestClient(app)
    body = {"a": f"upload/x1/{a.name}", "b": f"upload/x2/{b.name}", "page_a": 0, "page_b": None}
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    assert c.post("/feat/revcompare/cost", json=body).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "all")
    first = c.post("/feat/revcompare/compare", json=body).json()
    assert first["revision_cost"]["skirting"] == "pending"
    r = c.post("/feat/revcompare/cost", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["revision_cost"]["skirting"] == "done"
    again = c.post("/feat/revcompare/compare", json=body).json()  # cached: the compare now carries the skirting
    assert again["cached"] and again["revision_cost"]["skirting"] == "done"


def test_a_new_wall_on_a_scan_is_priced_as_approximate(sheets, tmp_path):
    a, b = sheets
    png = tmp_path / "rev_b_scan.png"
    to_scan(b, png)
    c = rc.compare(str(a), str(png))["revision_cost"]
    walls = [ln for ln in c["lines"] if ln["id"].startswith("wall:") and ln["delta"] > 0]
    assert len(walls) == 1 and walls[0]["approx"] and 5.0 < walls[0]["delta"] < 7.5, c["lines"]  # true 6.35 m
    assert "approximate" in walls[0]["detail"]
    assert any(v["id"] == "review:doors" for v in c["review"])  # door swings: vector pairs only, said so


def test_wall_length_in_a_redrawn_area_counts_each_wall_once():
    from types import SimpleNamespace

    ga = np.full((200, 600), 255, np.uint8)
    ga[50:62, 100:400] = 0  # a solid wall 300 px long, 12 px thick, on the old version only
    gb = np.full((200, 600), 255, np.uint8)
    gb[120:132, 100:500] = 0  # a 400 px wall on the new version
    gb[60:61, 100:400] = 0  # a 1-px line (a door leaf, a dimension): not a wall
    regions = [{"id": 1, "kind": "changed", "box_a": [90, 40, 510, 140]},
               {"id": 2, "kind": "changed", "box_a": [95, 45, 410, 70]}]  # overlaps region 1: must not count twice
    rc._wall_lengths(SimpleNamespace(gray=ga, zoom=1.0), gb, np.ones(ga.shape, bool), regions, m_per_unit=0.01)
    w1, w2 = regions[0]["walls_m"], regions[1]["walls_m"]
    assert abs(w1["old"] - 3.0) < 0.3 and abs(w1["new"] - 4.0) < 0.4, (w1, w2)  # the larger region claims first
    assert w2 == {"old": 0.0, "new": 0.0}
    c = rc.revision_cost({"a": {"kind": "vector"}, "b": {"kind": "vector"}, "approximate": False, "doors": None,
                          "areas": {}, "regions": [{**regions[0], "size_m": [4.2, 1.0], "near": "Hall"}]})
    (ln,) = c["lines"]
    assert ln["id"] == "walls:1" and ln["approx"] and abs(ln["delta"] - 1.0) < 0.5 and not c["review"]


def test_a_picture_stored_under_its_guessed_style_resolves(tmp_path, monkeypatch):
    up = tmp_path / "uploads"
    f = up / "0123456789ab" / "data" / "cubicasa5k" / "plan.png"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    monkeypatch.setattr(rc, "UPLOADS", up)
    assert rc.resolve("upload/0123456789ab/cubicasa5k/plan.png") == f.resolve()  # F8 22:42
    with pytest.raises(FileNotFoundError):
        rc.resolve("upload/0123456789ab/other/plan.png")
    with pytest.raises(ValueError):
        rc.resolve("upload/../x/plan.png")


def test_example_thumbnail_is_cut_where_most_changed(sheets, tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    cv2 = pytest.importorskip("cv2")
    from fastapi.testclient import TestClient

    img = np.full((900, 1600, 3), 255, np.uint8)
    img[100:110, 50:400] = (150, 150, 150)  # grey linework is not a change
    img[700:760, 1300:1500] = (40, 40, 220)  # red: removed, lower right
    x0, y0, x1, y1 = rc.change_window(img)
    assert x0 <= 1300 and x1 >= 1500 and y0 <= 700 and y1 >= 760
    assert abs((x1 - x0) / (y1 - y0) - 16 / 9) < 0.05
    assert rc.change_window(np.full((90, 160, 3), 255, np.uint8)) == (0, 0, 160, 90)  # nothing coloured: all of it

    a, b = sheets
    d = rc.DEMOS["dfe630"]
    (tmp_path / d["a"]).write_bytes(a.read_bytes())
    (tmp_path / d["b"]).write_bytes(b.read_bytes())
    monkeypatch.setattr(rc, "DEMO_DIR", tmp_path)
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    app = fastapi.FastAPI()
    app.include_router(rc.router)
    c = TestClient(app)
    monkeypatch.setenv("STOA_FEATURES", "revcompare")
    lst = {x["id"]: x for x in c.get("/feat/revcompare/demo").json()["demos"]}
    assert lst["dfe630"]["thumb"] == "/feat/revcompare/demo/dfe630/thumb.png"
    r = c.get(lst["dfe630"]["thumb"])  # not taken by /demo/{demo}/{which}
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    im = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert im.shape[1] == 960 and abs(im.shape[1] / im.shape[0] - 16 / 9) < 0.05
    assert c.get("/feat/revcompare/demo/nope/thumb.png").status_code == 404
    monkeypatch.delenv("STOA_FEATURES")
    assert c.get(lst["dfe630"]["thumb"]).status_code == 404  # off without the flag


def test_dust_specks_on_a_scan_are_not_a_change(sheets, tmp_path, monkeypatch):
    a, _ = sheets
    png = tmp_path / "rev_a_scan_dusty.png"
    to_scan(a, png)
    g = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
    for x, y in ((1600, 150), (1612, 146), (1606, 162)):  # three toner dots on empty paper (top right), px apart
        cv2.circle(g, (x, y), 1, 20, -1)
    cv2.imwrite(str(png), g)
    monkeypatch.setattr(rc, "SPECK", 0.0)
    assert rc.compare(str(a), str(png))["regions"], "the dots must be a mark without the speck rule (test is moot)"
    monkeypatch.setattr(rc, "SPECK", 0.0035)
    assert rc.compare(str(a), str(png))["regions"] == []


def test_a_thick_line_along_hatching_is_an_edge_not_a_partition():
    from types import SimpleNamespace

    g = np.full((400, 600), 255, np.uint8)
    g[100:104, 50:450] = 0  # run 1: 8 m long, hatching (lines every 6 px = 0.12 m) right below it: a roof edge
    for y in range(110, 150, 6):
        g[y:y + 2, 50:450] = 0
    g[250:254, 50:450] = 0  # run 2: the same line with a solid wall band below it (a window filled in): a wall
    g[258:268, 50:450] = 0
    a = SimpleNamespace(zoom=1.0, gray=g)
    regs = [{"id": 1, "kind": "removed", "box_a": [50, 100, 450, 104], "size_m": [8.0, 0.08]},
            {"id": 2, "kind": "removed", "box_a": [50, 250, 450, 254], "size_m": [8.0, 0.08]}]
    rc._mark_hatch_edges(a, g, regs, 0.02)  # 50 px per metre
    assert regs[0].get("beside_hatch") and not regs[1].get("beside_hatch")
    res = {"a": {"kind": "vector"}, "b": {"kind": "vector"}, "approximate": False, "doors": None,
           "areas": {"rooms": [], "by_name": []}, "regions": regs}
    c = rc.revision_cost(res)
    assert [x["id"] for x in c["review"]] == ["review:edges"]
    assert [x["delta"] for x in c["lines"] if x["group"] == "walls" and x.get("price")] == [-8.0]


def test_a_degenerate_fit_is_no_alignment():
    """Two unrelated sheets once gave a RANSAC fit that squeezed every match onto one spot (singular): LinAlgError."""
    assert not rc._sane(np.zeros((3, 3)))
    assert not rc._sane(np.array([[1e-4, 0, 5], [0, 1e-4, 5], [0, 0, 1]]))
    assert not rc._sane(np.array([[np.nan, 0, 0], [0, 1, 0], [0, 0, 1]]))
    assert rc._sane(np.array([[0.98, -0.02, 12], [0.02, 0.98, -7], [0, 0, 1]]))


def _cost_res(**kw):
    res = {"a": {"kind": "vector", "space": "pdf_pt"}, "b": {"kind": "vector", "space": "pdf_pt"},
           "alignment": {"overlap": 0.69}, "regions": [], "text": [],
           "areas": {"by_name": [{"name": "Yr 4", "unit": "m2", "count_old": 1, "count_new": 1, "old": [62.0], "new": [55.0],
                                  "sum_old": 62.0, "sum_new": 55.0, "delta": -7.0}], "rooms": []},
           "doors": None}
    res.update(kw)
    return res


def test_two_different_drawings_or_a_section_are_not_priced():
    """Another floor of the same school lines up ~0.6-0.78 (a real revision >= 0.92): its 'cost' was -EUR 30,556 ..
    -EUR 68,797. A section's long lines are floors and roofs, not partitions."""
    c = rc.revision_cost(_cost_res(other_sheet=True))
    assert c["total_eur"] == 0 and not c["lines"] and "different drawings" in c["withheld"]
    assert c["review"][0]["id"] == "review:withheld"
    c = rc.revision_cost(_cost_res(other_sheet=False, drawing_kind={"a": "elev", "b": "elev"}))
    assert not c["lines"] and "section" in c["withheld"]
    c = rc.revision_cost(_cost_res(other_sheet=False, drawing_kind={"a": "plan", "b": None}))
    assert c.get("withheld") is None and c["lines"] and c["total_eur"] < 0


def test_drawing_kind_reads_section_titles():
    s = rc.Sheet("x.pdf", "vector", "pdf_pt", np.full((10, 10), 255, np.uint8), 1.0, (10.0, 10.0),
                 lines=[(0, 0, 1, 1, "SECTION A-A"), (0, 0, 1, 1, "Scale 1:200 at A3")])
    assert rc.drawing_kind(s) == "elev"
    s.lines = [(0, 0, 1, 1, "3D LONG SECTION")]
    assert rc.drawing_kind(s) == "elev"
    s.lines = [(0, 0, 1, 1, "EXPLODED AXO")]
    assert rc.drawing_kind(s) == "elev"
    s.lines = [(0, 0, 1, 1, "FIRST FLOOR PLAN"), (0, 0, 1, 1, "Kitchen")]
    assert rc.drawing_kind(s) == "plan"


def test_skirting_accepts_a_trace_within_the_rounding_of_the_printed_area(monkeypatch):
    """A printed '13 m2' is 12.5-13.5: a 14.1 m2 trace (+4.4 % of 13.5) is the room; a '6 m2' traced 7.3 is not."""
    from stoa.evidence import lineplan, linework

    traced = {"SEN": 14.1, "Store": 7.3}
    monkeypatch.setattr(linework, "sheet_scales", lambda path, page: (200.0,))
    monkeypatch.setattr(lineplan, "_measure", lambda path, page, pt, n, box: {
        "area_m2": traced["SEN" if box[0] < 50 else "Store"],
        "polygon": [(0, 0), (10, 0), (10, 10), (0, 10)]})
    monkeypatch.setattr(lineplan, "_skirting", lambda path, page, n, m: {"value_m": 14.0, "perimeter_m": 15.0, "openings": [1]})
    res = {"a": {"kind": "vector", "space": "pdf_pt", "page": 0, "zoom": 1.0},
           "b": {"kind": "vector", "space": "pdf_pt", "page": 0, "zoom": 1.0},
           "alignment": {"matrix": np.eye(3).tolist()},
           "areas": {"rooms": [
               {"name": "SEN", "kind": "added", "old": None, "new": 13.0, "unit": "m2", "box_b": [10, 10, 30, 20],
                "box_b_in_a": [10, 10, 30, 20]},
               {"name": "Store", "kind": "added", "old": None, "new": 6.0, "unit": "m2", "box_b": [100, 10, 130, 20],
                "box_b_in_a": [100, 10, 130, 20]}]}}
    sk = rc.skirting_changes(res, "a.pdf", "b.pdf")
    assert [g["name"] for g in sk["by_name"]] == ["SEN"]
    assert [v["item"] for v in sk["review"]] == ["Skirting of Store"]


def test_skirting_follows_the_conventions_wet_rooms_and_whole_outline(monkeypatch):
    """F4's Conventions tab: tiled wet rooms get skirting only when included; each line carries the net run and the
    whole outline so 'door openings not deducted' needs no second trace."""
    from stoa.evidence import lineplan, linework

    monkeypatch.setattr(linework, "sheet_scales", lambda path, page: (100.0,))
    monkeypatch.setattr(rc, "_wet", lambda name: name == "Toilets")
    monkeypatch.setattr(lineplan, "_measure", lambda path, page, pt, n, box: {
        "area_m2": 20.0, "polygon": [(0, 0), (10, 0), (10, 10), (0, 10)]})
    monkeypatch.setattr(lineplan, "_skirting", lambda path, page, n, m: {"value_m": 17.0, "perimeter_m": 18.0, "openings": [1]})
    res = {"a": {"kind": "vector", "space": "pdf_pt", "page": 0, "zoom": 1.0},
           "b": {"kind": "vector", "space": "pdf_pt", "page": 0, "zoom": 1.0},
           "alignment": {"matrix": np.eye(3).tolist()},
           "areas": {"rooms": [{"name": n, "kind": "added", "old": None, "new": 20.0, "unit": "m2", "box_b": [x, 10, x + 20, 20],
                                "box_b_in_a": [x, 10, x + 20, 20]} for n, x in (("Office", 10), ("Toilets", 100))]}}
    dry = rc.skirting_changes(res, "a.pdf", "b.pdf")
    wet = rc.skirting_changes(res, "a.pdf", "b.pdf", wet=True)
    assert [g["name"] for g in dry["by_name"]] == ["Office"]
    assert sorted(g["name"] for g in wet["by_name"]) == ["Office", "Toilets"]
    res["areas"]["by_name"] = []
    lines = [ln for ln in rc.revision_cost(res, wet)["lines"] if ln["group"] == "skirting"]
    assert {ln["item"]: (ln["delta_net"], ln["delta_whole"], ln["wet"]) for ln in lines} == {
        "Office": (17.0, 18.0, False), "Toilets": (17.0, 18.0, True)}


def test_a_scanned_wall_run_is_measured_from_its_own_ink():
    """A 6 px thick new wall with two crossings (no NEW ink where it crosses old lines) and a 1-px residual tail
    along the wall it joins: the run is the wall's span, not the box (the box ran on 1.75 m on a turned scan)."""
    mask = np.zeros((40, 400), bool)
    mask[17:23, 10:60] = mask[17:23, 70:150] = mask[17:23, 165:200] = True  # the wall, crossed twice (10 / 15 px gaps)
    mask[20, 230:390] = True  # residual tail beyond a 30 px gap (> 1.2 m at 0.033 m/px)
    run = rc._ink_run_m(mask, [10, 17, 390, 23], 0.033)
    assert run == pytest.approx(190 * 0.033, abs=0.01)
    assert rc._ink_run_m(mask.T, [17, 10, 23, 390], 0.033) == run  # turned 90 degrees: the same
    assert rc._ink_run_m(np.zeros((10, 10), bool), [0, 0, 10, 10], 0.033) is None


@pytest.mark.parametrize("rot", [90, 180])
def test_room_tags_of_a_sheet_replotted_turned_are_read_upright(tmp_path, monkeypatch, rot):
    """The same plan re-plotted turned 90 / 180 degrees: its room tags are read on an upright copy and land on the
    turned page where the rooms are (the DfE first floor turned 90 degrees had 31 of 32 rooms 'changed')."""
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    up, turned = tmp_path / "up.pdf", tmp_path / f"turned{rot}.pdf"
    plan(str(up))
    with fitz.open(str(up)) as src:
        r = src[0].rect
        out = fitz.open()
        pg = out.new_page(width=r.height if rot == 90 else r.width, height=r.width if rot == 90 else r.height)
        pg.show_pdf_page(pg.rect, src, 0, rotate=rot)
        out.save(str(turned))
    a, b = rc.load_sheet(str(up)), rc.load_sheet(str(turned))
    assert rc._upright_copy(str(up), 0) is None
    ta = {t["name"]: t["value"] for t in rc.area_tags(a)}
    tb = rc.area_tags(b)
    assert {t["name"]: t["value"] for t in tb} == ta and ta.get("Kitchen") == 12
    al = rc.align(a, b)
    ar = rc.area_changes(a, b, al.M)
    assert ar["by_name"] == []  # nothing changed


def test_sheets_are_named_by_their_printed_title_number_and_revision(tmp_path):
    """UX NEW-11: the panel names the versions by what is printed on them, never a revision code as the sheet number."""
    def sheet(path, rev):
        doc = fitz.open()
        p = doc.new_page(width=842, height=595)
        p.insert_text((300, 60), "FIRST FLOOR PLAN", fontsize=18)
        for (x, y), t in (((640, 520), "SEN-DFE-XX-01-D-X-0202"), ((640, 540), "Revision"), ((640, 560), rev),
                          ((520, 560), "A-01")):
            p.insert_text((x, y), t, fontsize=8)
        doc.save(path)

    a, b = tmp_path / "plan-old.pdf", tmp_path / "plan-new.pdf"
    sheet(a, "C01")
    sheet(b, "C02")
    na, nb = rc.sheet_name(a, 0, "upload/0123456789ab/plan-old.pdf"), rc.sheet_name(b, 0)
    assert na["title"] == "First Floor Plan" and na["source"] == "sheet" and na["file"] == "plan-old.pdf"
    assert na["number"] == "SEN-DFE-XX-01-D-X-0202" and na["revision"] == "C01" and nb["revision"] == "C02"
    assert rc.list_sheets(a)[0]["sheet_no"] == "SEN-DFE-XX-01-D-X-0202"  # not 'C01' (set pairing used it)
    png = tmp_path / "IMG_2041.png"
    cv2.imwrite(str(png), np.full((40, 60), 255, np.uint8))
    n = rc.sheet_name(png)  # nothing readable: the file name
    assert n["source"] == "file" and n["title"] == "IMG 2041" and n["file"] == "IMG_2041.png"


def test_plain_plurals_and_the_examples_issue_names():
    assert rc._n(1, "label") == "1 label" and rc._n(3, "label") == "3 labels" and rc._n(2, "leaf", "leaves") == "2 leaves"
    res = {"a": {"name": {"file": rc.DEMOS["dfe630"]["a_name"], "title": "x"}},
           "b": {"name": {"file": rc.DEMOS["dfe630"]["b_name"], "title": "y"}}}
    rc._demo_names(res)
    assert res["a"]["name"]["version"].startswith("issue A") and res["b"]["name"]["title"] == "Primary school, first floor"
    s = rc.summarize({"regions": [{"kind": "added"}], "text": [{"kind": "moved"}] * 2,
                      "alignment": {"how": "by words", "overlap": 0.99}}, __import__("types").SimpleNamespace(method="text"))
    assert "1 linework change (" in s and "2 labels moved" in s and "(s)" not in s


def test_suggested_questions_ask_one_room_per_identical_change():
    """Yr 3-6 all 62 -> 55 m2: one question stands for them, so the Staff Room and a new room get asked too."""
    by = [{"name": n, "old": [62.0], "new": [55.0], "unit": "m2"} for n in ("Yr 3", "Yr 4", "Yr 5", "Yr 6")]
    by += [{"name": "Staff Room", "old": [49.0], "new": [65.0], "unit": "m2"},
           {"name": "Y6 Lockers", "old": [7.0], "new": [5.0], "unit": "m2"},
           {"name": "Practical Food", "old": [], "new": [41.0], "unit": "m2"}]
    for d in by:
        d["count_old"], d["count_new"] = len(d["old"]), len(d["new"])
    qs = [q["question"] for q in rc.suggest_questions({"areas": {"by_name": by}, "regions": []})]
    assert qs == ["What is the area of Yr 3?", "What is the area of the Staff Room?",
                  "What is the area of Y6 Lockers?", "What is the area of the Practical Food?"]
    qs = [q["question"] for q in rc.suggest_questions({"areas": {"by_name": by}, "regions": []}, max_rooms=3)]
    assert "What is the area of the Practical Food?" in qs and "What is the area of Y6 Lockers?" not in qs


def test_a_revision_replotted_turned_is_compared_on_its_upright_copy(sheets, tmp_path, monkeypatch):
    """The DfE first floor turned 90 / 180 deg was priced +EUR 326 / -EUR 2,028 (upright: +EUR 380): B is now
    compared on a copy turned back upright, so a turned re-plot finds what the upright one finds."""
    a, b = sheets
    up = tmp_path / "uploads" / "0123456789ab"
    up.mkdir(parents=True)
    (up / "a.pdf").write_bytes(a.read_bytes())
    with fitz.open(b) as d:
        for rot in (0, 180):
            nd = fitz.open()
            w, h = d[0].rect.width, d[0].rect.height
            nd.new_page(width=w, height=h).show_pdf_page(fitz.Rect(0, 0, w, h), d, 0, rotate=rot)
            for i in range(60):  # enough text for the 'most text runs upside down' test (the plan has ~30 words)
                nd[0].insert_text((20 + (i % 6) * 120, 20 + (i // 6) * 12), "NOTE TEXT", fontsize=6,
                                  rotate=rot)
            nd.save(up / f"b{rot}.pdf")
    monkeypatch.setattr(rc, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    straight = rc.cached_compare("upload/0123456789ab/a.pdf", "upload/0123456789ab/b0.pdf")
    turned = rc.cached_compare("upload/0123456789ab/a.pdf", "upload/0123456789ab/b180.pdf")
    assert turned["b"].get("frame") == "upright copy" and "upside down" in turned["page_b_note"]
    assert straight["b"].get("frame") is None and turned["b"]["file"] == "upload/0123456789ab/b180.pdf"
    assert turned["alignment"]["method"].startswith("text") and len(turned["regions"]) == len(straight["regions"])


def test_the_footer_of_a_sheet_drawn_from_a_dxf_is_never_a_change():
    """web/server/normalize_upload.py prints 'Sheet drawn from <file>.dxf at 1:N on A3 …' on a lone DXF upload's
    sheet: two revisions differ only by file name (and maybe scale); that line is ours, not the drawing's."""
    g = np.full((10, 10), 255, np.uint8)
    a = rc.Sheet("a.pdf", "vector", "pdf_pt", g, 1.0, (842.0, 595.0),
                 lines=[(30, 580, 400, 590, "Sheet drawn from flat-rev-A.dxf at 1:100 on A3 (no original sheet uploaded)"),
                        (100, 100, 140, 110, "12.09 m²")])
    b = rc.Sheet("b.pdf", "vector", "pdf_pt", g, 1.0, (842.0, 595.0),
                 lines=[(30, 580, 400, 590, "Sheet drawn from flat-rev-B.dxf at 1:200 on A3 (no original sheet uploaded)"),
                        (100, 100, 140, 110, "10.50 m²")])
    rows = rc.text_changes(a, b, np.eye(3)[:2])
    assert rows and not any("Sheet drawn from" in (t["old"] or "") + (t["new"] or "") for t in rows)
    assert {t["old"] for t in rows} | {t["new"] for t in rows} >= {"12.09 m²", "10.50 m²"}


def test_one_of_several_same_named_rooms_changed_is_named_as_such():
    """Swiss sheets name 13 rooms 'Zimmer': one changed reads '12.09 → 10.5 m² (1 of 13 rooms)', not a sum of 13."""
    d = {"name": "Zimmer", "unit": "m2", "old": [12.09, 8.0, 20.12], "new": [10.5, 8.0, 20.12], "count_old": 3,
         "count_new": 3, "sum_old": 40.21, "sum_new": 38.62, "delta": -1.59, "changed": [[12.09, 10.5]]}
    assert rc._partial(d, "m²") == "12.09 → 10.5 m² (1 of 3 rooms)"
    assert rc._partial({**d, "changed": [[12.09, 10.5], [8.0, 7.0], [20.12, 19.0]]}, "m²") is None  # all changed
    assert rc._partial({**d, "count_new": 2}, "m²") is None  # a room gone: the sums say it


def test_a_plain_decimal_in_the_corner_is_not_a_sheet_number_without_a_sheet_label():
    """Room numbers ('1.09' beside 'Computer Storage') and callouts ('2.02') were read as sheet numbers on 19 of the
    team's 232 sheet pages; '3.01.002' and 'A-101' still are, and '2.02' is beside 'Drawing No.'."""
    def page(words):
        doc = fitz.open()
        p = doc.new_page(width=842, height=595)
        for k, (w, size) in enumerate(words):
            p.insert_text((600 + 60 * (k % 3), 480 + 30 * (k // 3)), w, fontsize=size)
        p.insert_text((100, 100), "GROUND FLOOR PLAN", fontsize=20)
        return p
    assert rc._page_info(page([("Storage", 8), ("1.09", 12)]))["sheet_no"] is None
    assert rc._page_info(page([("A-101", 10), ("1.09", 12)]))["sheet_no"] == "A-101"
    assert rc._page_info(page([("3.01.002", 10)]))["sheet_no"] == "3.01.002"
    assert rc._page_info(page([("Drawing", 8), ("No.", 8), ("2.02", 12)]))["sheet_no"] == "2.02"


def test_two_versions_of_a_model_are_compared_by_element_id(tmp_path):
    """IFC: a door removed, a room's area changed and renamed, a wall moved -> exactly those; one room line (not gone +
    new); the door counts priced as door sets; a model vs a sheet is refused; two unrelated models are not priced."""
    import os
    from pathlib import Path

    ifcopenshell = pytest.importorskip("ifcopenshell")
    import ifcopenshell.api  # noqa: F401

    src = Path(os.environ.get("STOA_DATA", "")) / "data/ifc_bench/fzk_house_arc.ifc"
    if not src.exists():
        pytest.skip("dataset IFC not here")
    f = ifcopenshell.open(str(src))
    door = f.by_type("IfcDoor")[0]
    ifcopenshell.api.run("root.remove_product", f, product=door)
    sp = [s for s in f.by_type("IfcSpace") if s.LongName == "Schlafzimmer"][0]
    for rel in sp.IsDefinedBy:
        if rel.is_a("IfcRelDefinesByProperties") and rel.RelatingPropertyDefinition.is_a("IfcElementQuantity"):
            for q in rel.RelatingPropertyDefinition.Quantities:
                if q.is_a("IfcQuantityArea") and q.Name in ("NetFloorArea", "GrossFloorArea"):
                    q.AreaValue = q.AreaValue - 2.5
    sp.LongName = "Kinderzimmer"
    b = tmp_path / "house-B.ifc"
    f.write(str(b))
    r = rc.ifc_compare(src, b)
    ch = {(e["kind"], e["change"]) for e in r["model_changes"]["elements"]}
    assert ("door", "removed") in ch and ("room", "changed") in ch and not any(c == "added" for _, c in ch)
    assert [d["name"] for d in r["areas"]["by_name"]] == ["Schlafzimmer → Kinderzimmer"]
    c = r["revision_cost"]
    assert round(c["total_eur"]) == round(-2.5 * 38 - 450)
    assert not r["other_sheet"] and r["alignment"]["ok"]
    same = rc.ifc_compare(src, src)
    assert same["model_changes"]["n_elements"] == 0 and same["revision_cost"]["total_eur"] == 0


def test_a_cad_file_is_compared_as_the_sheet_beside_it_and_a_model_never_with_a_sheet(tmp_path, monkeypatch):
    from pathlib import Path

    dxf = tmp_path / "floor.dxf"
    dxf.write_text("0\nEOF\n")
    with pytest.raises(ValueError, match="CAD file"):
        rc._sheet_of_cad(dxf)
    (tmp_path / "floor.pdf").write_bytes(b"%PDF-1.4\n")
    assert rc._sheet_of_cad(dxf) == tmp_path / "floor.pdf"
    assert rc._sheet_of_cad(tmp_path / "x.png") == tmp_path / "x.png"
    m, s = tmp_path / "m.ifc", tmp_path / "s.pdf"
    m.write_text("ISO-10303-21;")
    s.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(rc, "resolve", lambda p: Path(p))
    with pytest.raises(ValueError, match="model"):
        rc.cached_compare(str(m), str(s))
