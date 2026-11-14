"""T3 project loop (stoa/features/project.py): levels, sheet roles, building totals without double counting, the
review list (tick, correction, open questions) kept on disk, CSV export, and the /feat/project router."""

from __future__ import annotations

import pytest

from stoa.features import project as P

UID = "0123456789ab"


def _pdf(path, text=""):
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    if text:
        pg.insert_text((600, 560), text, fontsize=9)
    d.save(str(path))
    d.close()


@pytest.fixture()
def up(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_PROJECTS_DIR", str(tmp_path / "projects"))
    names = ["plan_ground_floor.pdf", "plan_first_floor.pdf", "rcp_first_floor.pdf", "notes.png"]
    for n in names[:3]:
        _pdf(root / UID / n)
    from PIL import Image

    Image.new("RGB", (40, 30), "white").save(root / UID / names[3])
    return [f"upload/{UID}/{n}" for n in names]


def test_level_from_name():
    assert P.level_from_name("SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf") == ("1", "Level 01")
    assert P.level_from_name("SEC-DFE-XX-00-D-X-0201-x.pdf") == ("0", "Level 00")
    assert P.level_from_name("secondary_type_3_600_ground_floor_001.pdf")[0] == "0"
    assert P.level_from_name("Level_02.png")[0] == "2"
    assert P.level_from_name("house-2nd-floor.jpg")[0] == "2"
    assert P.level_from_name("A-L03.pdf")[0] == "3"
    assert P.level_from_name("plan GF.pdf")[0] == "0"
    assert P.level_from_name("ground_and_first_floor.pdf") == (None, None)  # names two levels
    assert P.level_from_name("site.pdf") == (None, None)
    assert P.level_from_name("ER-DFE-XX-XX-T-X-0301-Pattern_Book.pdf") == (None, None)  # XX = no level


def test_role_from_name():
    assert P.role_from_name("15720_section_aa.PDF") == "section"
    assert P.role_from_name("rcp_level_1.pdf") == "rcp"
    assert P.role_from_name("Primary_school_landscape_master_plan.pdf") == "site_plan"
    assert P.role_from_name("GA_Plan_Level_00.pdf") == "floor_plan"
    assert P.role_from_name("IMG_2041.jpg") is None


def test_project_id_is_the_upload_folder():
    assert P.project_id([f"upload/{UID}/a.pdf", f"upload/{UID}/b.png"]) == UID
    other = P.project_id([f"upload/{UID}/a.pdf", "upload/abcdefabcdef/b.png"])
    assert other.startswith("p") and len(other) == 15
    with pytest.raises(ValueError):
        P.store_path("../etc")


def test_resolve_refuses_outside(up):
    with pytest.raises(ValueError):
        P.resolve("/etc/passwd")
    with pytest.raises(ValueError):
        P.resolve("upload/../../x")
    assert P.resolve(up[0]).endswith("plan_ground_floor.pdf")


def test_open_classifies_levels_and_roles(up):
    v = P.view(P._open(up))
    assert v["id"] == UID
    by = {s["file"].split("/")[-1]: s for s in v["sheets"]}
    assert by["plan_ground_floor.pdf"]["kind"] == "floor_plan" and by["plan_ground_floor.pdf"]["level"]
    assert by["plan_first_floor.pdf"]["storey"] == "1"
    assert by["rcp_first_floor.pdf"]["kind"] == "rcp"
    assert by["notes.png"]["level"] == ""
    assert [lv["storey"] for lv in v["levels"]][:2] == ["0", "1"]
    assert P.store_path(UID).exists()  # next to the uploads, like F7's memory


def _parts(plan, values, unit="count", note="door"):
    return [{"sheet": s["id"], "answer": {"value": v, "unit": unit, "confidence": 0.7, "solver": "x",
                                          "evidence": [{"file": s["file"], "page": 0, "bbox": [1, 2, 3, 4], "note": note}],
                                          "cost": {"wall_seconds": 1.5, "api_calls": 0}}}
            for s, v in zip(plan["sheets"], values)]


def test_building_total_skips_ceiling_plans_and_sums(up):
    proj = P._open(up[:3])
    pl = P.plan(proj, "How many doors are there in the whole building?")
    names = [s["file"].split("/")[-1] for s in pl["sheets"]]
    assert "rcp_first_floor.pdf" not in names and len(names) == 2
    assert any("ceiling" in s["why"] for s in pl["skipped"])
    a = P.combine(proj, "How many doors are there in the whole building?", _parts(pl, [12, 9]))
    assert a["value"] == 21 and a["unit"] == "count" and a["confidence"] <= P.TOTAL_CAP
    assert len(a["evidence"]) == 2 and all(":" in e["note"] for e in a["evidence"])
    assert a["cost"]["wall_seconds"] == 3.0


def test_no_total_when_a_level_has_no_number_or_two_values(up):
    proj = P._open(up[:3])
    pl = P.plan(proj, "How many doors in the whole building?")
    a = P.combine(proj, "q", _parts(pl, [12, None]))
    assert a["value"] is None and "no number" in a["no_total_why"] and a["per_sheet"][0]["value"] == 12
    # the user says both sheets are Level 01: the same level twice with different values -> no total
    for s in proj["sheets"][:2]:
        s["user"] = {"level": "Level 01"}
    a = P.combine(proj, "q", _parts(P.plan(proj, "q whole building"), [12, 9]))
    assert a["value"] is None and "two sheets for" in a["no_total_why"]
    # ... and the same value twice: counted once
    a = P.combine(proj, "q", _parts(P.plan(proj, "q whole building"), [12, 12]))
    assert a["value"] == 12


def test_mixed_units_convert(up):
    proj = P._open(up[:2])
    pl = P.plan(proj, "What is the total floor area of the whole building?")
    parts = _parts(pl, [100.0, 1076.391], unit="m2")
    parts[1]["answer"]["unit"] = "ft2"
    a = P.combine(proj, "q", parts)
    assert a["unit"] == "m2" and abs(a["value"] - 200.0) < 0.01


def test_repeated_printed_total_counted_once(up):
    proj = P._open(up[:2])
    pl = P.plan(proj, "How many dorm rooms in the whole building?")
    a = P.combine(proj, "q", _parts(pl, [144, 144], note="TOTAL DORM ROOMS 144"))
    assert a["value"] == 144


def test_review_list_correction_survives_reload(up):
    proj = P._open(up[:3])
    it = P.upsert_item(proj, P._item_from("How big is the hall?", {"value": 48.7, "unit": "m2", "confidence": 0.4,
                                                                   "evidence": []}, "sheet", up[0], 0, "GF", "Level 00"))
    P.upsert_item(proj, P._item_from("How many lifts?", {"value": None, "missing": ["no lift symbol"]}, "sheet", up[1]))
    P.save(UID, proj)
    proj = P._get(UID)
    P.patch_item(proj, it["id"], {"value": "32.05", "unit": "m2", "note": "measured by hand"})
    P.save(UID, proj)
    v = P.view(P._get(UID))  # reload from disk
    got = next(i for i in v["items"] if i["id"] == it["id"])
    assert got["corrected"]["value"] == 32.05 and got["checked"] is True
    assert [o["question"] for o in v["open"]] == ["How many lifts?"] and "lift" in v["open"][0]["why_open"]
    # the same question again with the same value keeps the user's tick and correction
    again = P.upsert_item(proj, P._item_from("How big is the hall?", {"value": 48.7, "unit": "m2"}, "sheet", up[0]))
    assert again["id"] == it["id"] and again["corrected"]["value"] == 32.05
    csv = P.to_csv(proj)
    assert "32.05" in csv.splitlines()[1] and csv.startswith("scope,level,sheet")


def test_user_level_names_win(up):
    proj = P._open(up)
    sid = next(s["id"] for s in proj["sheets"] if s["file"].endswith("notes.png"))
    for s in proj["sheets"]:
        if s["id"] == sid:
            s["user"] = {"level": "Level 2", "role": "Floor plan"}
    e = next(P.effective(s) for s in proj["sheets"] if s["id"] == sid)
    assert e["storey"] == "2" and e["level_source"] == "you" and e["kind"] == "floor_plan"


def test_router_round_trip(up):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(P.router)
    c = TestClient(app)
    v = c.post("/feat/project/open", json={"files": up[:3]}).json()
    assert v["id"] == UID and len(v["sheets"]) == 3
    pl = c.post(f"/feat/project/{UID}/plan", json={"question": "How many doors in the whole building?"}).json()
    r = c.post(f"/feat/project/{UID}/combine", json={"question": "How many doors in the whole building?",
                                                     "parts": _parts(pl, [5, 7])}).json()
    assert r["answer"]["value"] == 12 and r["item"]["scope"] == "building"
    iid = r["item"]["id"]
    assert c.post(f"/feat/project/{UID}/items/{iid}", json={"value": 13, "note": "one door hidden"}).status_code == 200
    sid = v["sheets"][0]["id"]
    assert c.post(f"/feat/project/{UID}/sheet", json={"sheet": sid, "level": "Ground floor"}).status_code == 200
    g = c.get(f"/feat/project/{UID}").json()
    assert g["items"][0]["corrected"]["value"] == 13 and g["sheets"][0]["level"] == "Ground floor"
    assert c.get(f"/feat/project/{UID}/export").text.count("\n") == 2
    assert c.get("/feat/project/zzz").status_code == 400
    assert c.post("/feat/project/open", json={"files": ["/etc/passwd"]}).status_code == 400


def test_sheet_question():
    sq = P.sheet_question
    assert sq("How many doors are there in the whole building?") == "How many doors are there on this plan?"
    assert sq("How many doors does the building have?") == "How many doors are there on this plan?"
    assert sq("What is the total floor area across all floors?") == "What is the total floor area on this plan?"
    assert sq("How many toilets are in the school in total?") == "How many toilets are on this plan?"
    assert sq("What is the total area of the whole building's classrooms?") == \
        "What is the total area of the classrooms on this plan?"
    assert sq("Count the windows of the entire school.") == "Count the windows on this plan."
    assert sq("How big is the kitchen?") == "How big is the kitchen?"  # no building scope: unchanged
    assert sq("How many doors are on this plan in the whole building?") == "How many doors are on this plan?"


def test_lifts_are_not_summed_over_levels(up):
    proj = P._open(up[:2])
    pl = P.plan(proj, "How many lifts are there in the whole building?")
    a = P.combine(proj, "How many lifts are there in the whole building?", _parts(pl, [2, 1]))
    assert a["value"] == 2 and a["confidence"] <= 0.5 and "not added" in a["trace"][0]
    assert [r["note"] for r in a["per_sheet"]][1].startswith("not added")
    # areas of stairs per level are floor area: those are added
    a = P.combine(proj, "What is the total area of the stairs in the whole building?", _parts(pl, [10.0, 12.0], unit="m2"))
    assert a["value"] == 22.0


def test_rough_answers_are_open(up):
    proj = P._open(up[:2])
    it = P.upsert_item(proj, P._item_from("How many parking spaces?", {"value": 0, "unit": "count", "confidence": 0.27},
                                          "sheet", up[0]))
    assert it["status"] == "open" and "rough guess" in it["why_open"]
    P.patch_item(proj, it["id"], {"value": 12})
    assert it["status"] == "answered" and it["corrected"]["value"] == 12


def test_one_sheet_without_level_is_summed_with_a_warning(up):
    proj = P._open([up[0], up[3]])  # ground floor + a picture nothing could be read from
    pl = P.plan(proj, "How many doors in the whole building?")
    assert len(pl["sheets"]) == 2
    a = P.combine(proj, "q", _parts(pl, [3, 4]))
    assert a["value"] == 7 and any("no level read" in x for x in a["assumptions"])


def test_cover_and_partial_pages(tmp_path, monkeypatch):
    import pymupdf

    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_PROJECTS_DIR", str(tmp_path / "projects"))
    d = pymupdf.open()
    for text in ("COVER SHEET\nINDEX OF DRAWINGS\nFIRST FLOOR PLAN\nSECOND FLOOR PLAN", "FIRST FLOOR PLAN",
                 "SECOND FLOOR PLAN", "SECOND FLOOR COMMON AREA PLAN", "PLAN"):
        pg = d.new_page(width=842, height=595)
        y = 60
        for ln in text.split("\n"):
            pg.insert_text((60, y), ln, fontsize=14)
            y += 30
    d.save(str(root / UID / "set.pdf"))
    proj = P._open([f"upload/{UID}/set.pdf"])
    kinds = [s["kind"] for s in proj["sheets"]]
    assert kinds[0] == "cover"
    pl = P.plan(proj, "How many doors in the whole building?")
    pages = [s["page"] for s in pl["sheets"]]
    assert 0 not in pages and 3 not in pages, (pages, pl["skipped"])


def test_users_value_coming_back_keeps_the_original_reading(up):
    proj = P._open(up[:2])
    it = P.upsert_item(proj, P._item_from("How big is the hall?", {"value": 243.39, "unit": "m2", "confidence": 0.6},
                                          "sheet", up[0]))
    P.patch_item(proj, it["id"], {"value": 241, "unit": "m2"})
    back = P.upsert_item(proj, P._item_from("How big is the hall?", {"value": 241.0, "unit": "m2", "confidence": 0.99},
                                            "sheet", up[0]))
    assert back["value"] == 243.39 and back["corrected"]["value"] == 241 and back["checked"] is True


def test_route_to_the_level_the_question_names(up):
    proj = P._open(up[:3])
    gf, ff = (s["id"] for s in proj["sheets"][:2])
    assert P.level_in_question("How many doors are on the first floor?") == "1"
    assert P.level_in_question("How many doors are on Level 00?") == "0"
    assert P.level_in_question("How many doors are on every floor?") is None
    r = P.route(proj, "How many doors are on the first floor?", gf)
    assert r and r["id"] == ff  # the ceiling plan of level 1 is never the target
    assert P.route(proj, "How many doors are on the first floor?", ff) is None  # already there
    assert P.route(proj, "How big is the kitchen?", gf) is None


def test_xlsx_export_through_f2_writer(up):
    import io
    import zipfile

    pytest.importorskip("stoa.features.takeoff")
    proj = P._open(up[:2])
    pl = P.plan(proj, "How many doors in the whole building?")
    P.upsert_item(proj, P._item_from("How many doors in the whole building?", P.combine(proj, "q", _parts(pl, [3, 4])),
                                     "building"))
    data = P.to_xlsx(proj)
    assert data is not None
    z = zipfile.ZipFile(io.BytesIO(data))
    assert b"Per level" in z.read("xl/workbook.xml")
    assert b"How many doors" in z.read("xl/worksheets/sheet1.xml") and b"added" in z.read("xl/worksheets/sheet2.xml")


# ----------------------------------------------------------------------------------- copies (the same sheet twice)
def _plan_pdf(path, rooms, title="", number=""):
    """A line-drawn 'plan': rectangles at the given (x, y, w, h) in points + an optional title block line."""
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    for x, y, w, h in rooms:
        pg.draw_rect(pymupdf.Rect(x, y, x + w, y + h), color=(0, 0, 0), width=2.5)
        pg.draw_line(pymupdf.Point(x, y + h / 2), pymupdf.Point(x + w, y + h / 2), color=(0, 0, 0), width=1.5)
    if title:
        pg.insert_text((560, 560), title, fontsize=9)
    if number:
        pg.insert_text((560, 575), number, fontsize=9)
    d.save(str(path))
    d.close()


def _scan_of(pdf_path, png_path, shift=(6, 4)):
    """A 'scan' of a PDF: rendered, shifted, greyed paper, a dark scanner edge, noise (no text layer)."""
    import numpy as np
    import pymupdf
    from PIL import Image

    with pymupdf.open(str(pdf_path)) as d:
        pix = d[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), colorspace=pymupdf.csGRAY)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    a = np.roll(a, shift, axis=(0, 1)) * 0.85 + 20
    a[:, :8] = 40  # the scanner's dark edge
    a += np.random.default_rng(0).normal(0, 6, a.shape)
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(str(png_path))


L0 = [(60, 60, 200, 120), (260, 60, 120, 120), (60, 180, 320, 90), (420, 60, 90, 210), (160, 300, 120, 140)]
L1 = [(60, 60, 320, 70), (60, 130, 100, 140), (160, 130, 220, 140), (420, 60, 90, 120), (420, 180, 90, 90)]


def test_a_scan_of_an_uploaded_plan_is_a_copy_counted_once(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    _plan_pdf(root / UID / "A-100.pdf", L0, "GROUND FLOOR PLAN")
    _plan_pdf(root / UID / "A-101.pdf", L1, "FIRST FLOOR PLAN")
    _scan_of(root / UID / "A-100.pdf", root / UID / "IMG_0042.png")
    files = [f"upload/{UID}/{n}" for n in ("A-100.pdf", "A-101.pdf", "IMG_0042.png")]
    proj = P.build(files)
    by = {s["file"].split("/")[-1]: P.effective(s) for s in proj["sheets"]}
    scan = by["IMG_0042.png"]
    assert scan["medium"] == "image" and scan["copy_of"] == f"upload/{UID}/A-100.pdf#0", scan
    assert scan["storey"] == by["A-100.pdf"]["storey"] and scan["level_source"].startswith("sheet it copies")
    assert scan["copy_score"] >= P.COPY_LOOK
    pl = P.plan(proj, "How many rooms are there in the whole building?")
    assert [s["file"].split("/")[-1] for s in pl["sheets"]] == ["A-100.pdf", "A-101.pdf"]
    assert any("copy of" in s["why"] for s in pl["skipped"])
    # the user ticks the copy in: then it is asked like any sheet
    proj["sheets"][2]["user"] = {"include": True}
    assert len(P.plan(proj, "How many rooms are there in the whole building?")["sheets"]) == 3
    # a level question never routes to the copy
    proj["sheets"][2]["user"] = {}
    r = P.route(proj, "How many rooms are on the ground floor?", current=f"upload/{UID}/A-101.pdf#0")
    assert r is None or r["file"].endswith("A-100.pdf")


def test_a_scan_of_another_floor_is_not_a_copy(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    _plan_pdf(root / UID / "A-100.pdf", L0, "GROUND FLOOR PLAN")
    _plan_pdf(tmp_path / "other.pdf", L1, "FIRST FLOOR PLAN")
    _scan_of(tmp_path / "other.pdf", root / UID / "IMG_0043.png")
    proj = P.build([f"upload/{UID}/A-100.pdf", f"upload/{UID}/IMG_0043.png"])
    scan = next(s for s in proj["sheets"] if s["file"].endswith(".png"))
    assert not scan.get("copy_of"), scan


def test_same_drawing_number_is_a_copy_ocr_tolerant():
    sheets = [
        {"id": "a.pdf#0", "file": "a.pdf", "page": 0, "number": "SEN-DFE-XX-00-D-X-0201", "kind": "floor_plan",
         "role": "Floor plan", "kind_source": "title block", "storey": "0", "level": "Level 00", "medium": "vector"},
        {"id": "b.png#0", "file": "b.png", "page": 0, "number": "SEN-DFE-XX-0O-D-X-020l", "kind": "unknown",
         "role": "Drawing", "kind_source": "not read", "storey": None, "level": "", "medium": "image"},
        {"id": "c.pdf#0", "file": "c.pdf", "page": 0, "number": "SEN-DFE-XX-01-D-X-0202", "kind": "floor_plan",
         "role": "Floor plan", "kind_source": "title block", "storey": "1", "level": "Level 01", "medium": "vector"},
    ]
    P.find_copies(sheets)
    assert sheets[1]["copy_of"] == "a.pdf#0" and "drawing number" in sheets[1]["copy_how"]
    assert sheets[1]["storey"] == "0" and sheets[1]["kind"] == "floor_plan"
    assert not sheets[2].get("copy_of") and not sheets[0].get("copy_of")
    # one number, two different levels read: not a copy
    sheets[1].update(number="SEN-DFE-XX-00-D-X-0201", storey="2", level="Level 02")
    P.find_copies(sheets)
    assert not sheets[1].get("copy_of")


def test_several_rooms_of_one_name_is_an_open_question_the_user_settles():
    ans = {"value": 51.74, "unit": "m2", "confidence": 0.45, "solver": "pipeline/plan_rooms",
           "assumptions": ["scale: 1:200 printed on the sheet",
                           "3 rooms are labelled 'Secondary Classroom' and measure alike (52.13, 52.29, 50.77): their mean"],
           "evidence": [{"file": "f.pdf", "page": 0, "bbox": [1, 2, 3, 4], "note": "Secondary Classroom: 52.13 m² traced at 1:200"},
                        {"file": "f.pdf", "page": 0, "bbox": [5, 6, 7, 8], "note": "Secondary Classroom: 52.29 m² traced at 1:200"},
                        {"file": "f.pdf", "page": 0, "bbox": [9, 9, 9, 9], "note": "Secondary Classroom: 50.77 m² traced at 1:200"}]}
    it = P._item_from("How big is the classroom?", ans, "sheet", "f.pdf", 0, "L00", "Level 00")
    assert it["status"] == "open" and it["ambiguous"]["label"] == "Secondary Classroom"
    assert [(c["n"], c["value"], c["unit"]) for c in it["ambiguous"]["choices"]] == [(1, 52.13, "m2"), (2, 52.29, "m2"),
                                                                                     (3, 50.77, "m2")]
    assert "Which one" in it["why_open"]
    proj = {"items": []}
    P.upsert_item(proj, it)
    P.patch_item(proj, it["id"], {"value": 52.29, "unit": "m2", "note": "you picked mark 2"})
    got = proj["items"][0]
    assert got["status"] == "answered" and got["checked"] and got["corrected"]["value"] == 52.29
    # the same reading again keeps the pick
    P.upsert_item(proj, P._item_from("How big is the classroom?", ans, "sheet", "f.pdf", 0, "L00", "Level 00"))
    assert proj["items"][0]["corrected"]["value"] == 52.29 and proj["items"][0]["status"] == "answered"
    # 'Keep the mean' on a fresh one stays resolved when the same reading comes back
    proj2 = {"items": []}
    it2 = P.upsert_item(proj2, P._item_from("How big is the classroom?", ans, "sheet", "f.pdf", 0, "L00", "Level 00"))
    P.patch_item(proj2, it2["id"], {"resolved": True})
    P.upsert_item(proj2, P._item_from("How big is the classroom?", ans, "sheet", "f.pdf", 0, "L00", "Level 00"))
    assert proj2["items"][0]["status"] == "answered" and "kept the mean" in proj2["items"][0]["why_open"]
    # one room of that name, or no per-room values: a plain answer
    one = dict(ans, assumptions=["scale: 1:200 printed on the sheet"])
    assert P._item_from("How big is the hall?", one, "sheet", "f.pdf")["status"] == "answered"


def test_same_level_and_title_scan_is_a_copy():
    sheets = [
        {"id": "a.pdf#0", "file": "a.pdf", "page": 0, "number": "", "title": "Secondary Type 3 600 First Floor",
         "kind": "floor_plan", "role": "Floor plan", "kind_source": "title block", "storey": "1", "level": "First floor",
         "medium": "vector"},
        {"id": "b.png#0", "file": "b.png", "page": 0, "number": "", "title": "Secondary Type 3 6OO First Fl00r",
         "kind": "floor_plan", "role": "Floor plan", "kind_source": "title block", "storey": "1", "level": "First floor",
         "medium": "image"},
        {"id": "c.png#0", "file": "c.png", "page": 0, "number": "", "title": "Secondary Type 3 600 Second Floor",
         "kind": "floor_plan", "role": "Floor plan", "kind_source": "title block", "storey": "2", "level": "Second floor",
         "medium": "image"},
    ]
    P.find_copies(sheets)
    assert sheets[1]["copy_of"] == "a.pdf#0" and sheets[1]["title"] == "Secondary Type 3 600 First Floor"
    assert not sheets[2].get("copy_of")


def test_whole_floor_area_questions():
    yes = ["What is the total floor area of the school?", "What is the total floor area on this plan?",
           "What is the total area of all rooms on this plan?", "What is the gross internal floor area?",
           "What is the GIA of the building?", "What is the floor area of the whole building?", "Total floor area?"]
    no = ["What is the total floor area of all Group Rooms?", "What is the total area of the classrooms?",
          "What is the floor area of the Multi-Purpose Hall?", "total area of the kitchen", "How many doors are there?"]
    assert all(P.is_floor_area_question(q) for q in yes), [q for q in yes if not P.is_floor_area_question(q)]
    assert not any(P.is_floor_area_question(q) for q in no), [q for q in no if P.is_floor_area_question(q)]
    assert P.plan({"sheets": []}, yes[0])["floor_area"] is True


def test_floor_area_sum_is_said_in_the_building_answer():
    proj = {"sheets": [
        {"id": "a.pdf#0", "file": "a.pdf", "page": 0, "number": "", "title": "L00", "kind": "floor_plan", "role": "Floor plan",
         "kind_source": "title block", "storey": "0", "level": "Level 00", "building": "", "partial": False, "user": {}},
        {"id": "b.pdf#0", "file": "b.pdf", "page": 0, "number": "", "title": "L01", "kind": "floor_plan", "role": "Floor plan",
         "kind_source": "title block", "storey": "1", "level": "Level 01", "building": "", "partial": False, "user": {}}]}
    part = lambda v: {"value": v, "unit": "m2", "confidence": 0.5, "solver": "project/rooms_sum",
                      "assumptions": [f"net floor area: the sum of 40 rooms ({v})"], "evidence": []}
    a = P.combine(proj, "What is the total floor area of the school?",
                  [{"sheet": "a.pdf#0", "answer": part(1600.0)}, {"sheet": "b.pdf#0", "answer": part(830.0)}])
    assert a["value"] == 2430.0 and a["unit"] == "m2"
    assert any("Level 00: net floor area" in x for x in a["assumptions"])
    assert any("walls are not included" in x for x in a["assumptions"])


def test_floor_area_on_the_dfe_sen_sheet():
    import os

    pdf = (".stoa/datasets/stoa-like/dfe/"
           "SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf")
    if not os.path.exists(pdf):
        pytest.skip("DfE SEN sheet not on this machine")
    a = P.floor_area(pdf, "upload/0123456789ab/L01.pdf", 0)
    assert a["value"] and 700 < a["value"] < 950 and a["solver"] == "project/rooms_sum" and not a["fallback"]
    assert len(a["evidence"]) >= 30 and all(e.get("polygon") for e in a["evidence"])


def test_a_printed_gifa_table_wins_over_the_traced_sum(tmp_path):
    import pymupdf

    pdf = tmp_path / "gf.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    for i, (lab, val) in enumerate([("Ground:", "2398m2"), ("First:", "1172m2"), ("Second:", "1274m2"),
                                    ("Total:", "4844m2")]):
        pg.insert_text((86, 480 + 7 * i), lab, fontsize=5)
        pg.insert_text((117, 480 + 7 * i), val, fontsize=5)
    pg.insert_text((86, 465), "Overall GIFA", fontsize=5)
    pg.insert_text((86, 515), "TARGET: 4844m2", fontsize=5)
    d.save(str(pdf))
    d.close()
    pr = P.printed_floor_areas(str(pdf))
    assert pr["kind"] == "GIFA" and pr["levels"]["0"][0] == 2398 and pr["levels"]["2"][0] == 1274
    assert pr["total"][0] == 4844
    a = P.floor_area(str(pdf), "upload/0123456789ab/gf.pdf", 0, "1")
    assert a["value"] == 1172 and a["solver"] == "project/printed_area" and a["evidence"][0]["bbox"]
