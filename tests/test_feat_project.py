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
    assert r["question"] == "How many doors are on this plan?" and r["here"] is False
    here = P.route(proj, "How many doors are on the first floor?", ff)  # already there: asked as '… on this plan?'
    assert here["here"] is True and here["id"] == ff and here["question"] == "How many doors are on this plan?"
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
    a = P.floor_area(pdf, "upload/0123456789ab/L01.pdf", 0, None, "What is the net floor area of all rooms?")  # net = rooms
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


def _txt_pdf(path, words):
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    for i, w in enumerate(words):
        pg.insert_text((60, 60 + 18 * i), w, fontsize=9)
    d.save(str(path))
    d.close()


def test_one_named_room_is_not_summed_over_levels(up):
    """M1 21:36: 'What is the floor area of the Multi-Purpose Hall?' in building scope was 244.6 (L00) + 55.72 (L01).
    The room is printed on one level only: that level answers; a room printed on two levels = which one, no total."""
    from pathlib import Path

    root = Path(P.uploads_root()) / UID
    _txt_pdf(root / "plan_ground_floor.pdf", ["MULTI-PURPOSE", "HALL", "CLASSROOM", "KITCHEN", "GROUND FLOOR PLAN"])
    _txt_pdf(root / "plan_first_floor.pdf", ["CLASSROOM", "STAFF ROOM", "VOID", "FIRST FLOOR PLAN"])
    proj = P._open(up[:2], refresh=True)
    q = "What is the floor area of the Multi-Purpose Hall?"
    pl = P.plan(proj, q)
    assert [s["file"].split("/")[-1] for s in pl["sheets"]] == ["plan_ground_floor.pdf"]
    assert pl["room"]["name"] == "Multi-Purpose Hall"
    assert any("is not printed on this sheet" in s["why"] for s in pl["skipped"])
    ids = [s["id"] for s in proj["sheets"] if s["file"].endswith(("ground_floor.pdf", "first_floor.pdf"))]
    two = [{"sheet": ids[0], "answer": {"value": 244.6, "unit": "m2", "confidence": 0.7}},
           {"sheet": ids[1], "answer": {"value": 55.72, "unit": "m2", "confidence": 0.7}}]
    a = P.combine(proj, q, two)
    assert a["value"] == 244.6 and "printed on" in a["assumptions"][0]
    a = P.combine(proj, "How big is the classroom?", two)  # on both levels: which one, never 244.6 + 55.72
    assert a["value"] is None and "which one" in a["no_total_why"]
    # a kind of room / the whole building is still summed
    assert P.plan(proj, "How many classrooms does the school have?")["room"] is None
    assert P.plan(proj, "What is the total area of all classrooms in the school?")["room"] is None
    assert P.combine(proj, "How many doors are there in the whole building?",
                     [dict(p, answer={"value": n, "unit": "count", "confidence": 0.6}) for p, n in zip(two, (5, 7))])["value"] == 12


def test_review_list_is_kept_per_visitor(up):
    """QA 21:45 / 21:47: content-keyed uploads made every visitor share one review list."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(P.router)
    c = TestClient(app)
    a, b = "visitorAAAA1111", "visitorBBBB2222"
    va = c.post(f"/feat/project/open?visitor={a}", json={"files": up[:2]}).json()
    vb = c.post(f"/feat/project/open?visitor={b}", json={"files": up[:2]}).json()
    assert va["id"] == vb["id"] == UID
    sid = va["sheets"][0]["id"]
    r = c.post(f"/feat/project/{UID}/items?visitor={a}", json={"question": "How big is the kitchen?", "file": sid.split("#")[0],
                                                              "page": 0, "answer": {"value": 12.5, "unit": "m2", "confidence": 0.7}})
    iid = r.json()["item"]["id"]
    assert c.post(f"/feat/project/{UID}/items/{iid}?visitor={a}", json={"value": 13.0, "note": "measured"}).status_code == 200
    assert len(c.get(f"/feat/project/{UID}?visitor={a}").json()["items"]) == 1
    assert c.get(f"/feat/project/{UID}?visitor={b}").json()["items"] == []  # the other browser sees none of it
    assert c.get(f"/feat/project/{UID}").status_code == 404  # nor a shared (no-id) list: none was made
    assert c.post(f"/feat/project/{UID}/items/{iid}?visitor={b}", json={"value": 1}).status_code == 404
    assert P.visitor_key("../../etc") == "" and P.visitor_key("short") == ""
    assert "13" in c.get(f"/feat/project/{UID}/export?visitor={a}").text


def test_floor_area_gross_first_net_on_request(up, monkeypatch):
    """A whole-floor area: a printed GIFA line first, then the drawn outline's GIA (VP's footprint), the traced rooms'
    net sum only for 'net' / 'all rooms' questions (and as the GIA's cross-check once known)."""
    import sys
    import types

    from stoa.contract import Answer, Evidence

    fake = types.ModuleType("stoa.sheet.footprint")
    fake.footprint_answer = lambda path, q, page=0: Answer(
        value=1670.8, unit="m2", confidence=0.6, solver="footprint", assumptions=["scale 1:200"],
        evidence=[Evidence(file=path, page=page, bbox=[0, 0, 10, 10], note="building outline")])
    monkeypatch.setitem(sys.modules, "stoa.sheet.footprint", fake)
    calls = []

    def rooms(abs_path, virtual, page, key):
        calls.append(key)
        out = {"value": 1608.53, "unit": "m2", "confidence": 0.5, "solver": "project/rooms_sum", "rooms": 77,
               "assumptions": ["net floor area: the sum of 77 rooms"], "evidence": [], "fallback": False}
        P._FA_CACHE[key] = out
        return out

    monkeypatch.setattr(P, "_rooms_sum", rooms)
    P._FA_CACHE.clear()
    ab = P.resolve(up[0])
    g = P.floor_area(ab, up[0], 0, "0", "What is the total floor area of the school?")
    assert g["value"] == 1670.8 and g["solver"] == "project/gross_area" and not calls
    assert g["evidence"][0]["file"] == up[0] and "GIA" in g["assumptions"][0]
    n = P.floor_area(ab, up[0], 0, "0", "What is the net floor area of all rooms?")
    assert n["value"] == 1608.53 and len(calls) == 1
    g = P.floor_area(ab, up[0], 0, "0", "What is the total floor area of the school?")
    assert any(x.startswith("cross-check: its 77 traced rooms add up to 1,608.53") for x in g["assumptions"])
    P._FA_CACHE.clear()


def test_a_number_every_sheet_prints_is_not_a_copy_rule():
    """F8 22:27: HAER HI-76 scans C3 and C6 both print 'HAER No. HI-76' in the number slot: different titles = two
    drawings, not copies (a copy would be left out of the building totals). A scan of the PDF (OCR'd title) still is."""
    def sh(i, file, number, title, medium, storey=None):
        return {"id": f"{file}#0", "file": file, "page": 0, "number": number, "title": title, "medium": medium,
                "storey": storey, "level": "", "level_source": "", "kind": "floor_plan", "role": "Floor plan",
                "kind_source": "title block", "building": ""}

    a = sh(0, "upload/x/00011a.jpg", "HAER No. HI-76", "GC1 PUMP STATION PLAN AND SECTIONS", "scan")
    b = sh(1, "upload/x/00014a.jpg", "HAER No. HI-76", "BUILDING 43 ROOF PLAN AND DETAILS", "scan")
    P.find_copies([a, b])
    assert not a.get("copy_of") and not b.get("copy_of")
    pdf = sh(0, "upload/x/L00.pdf", "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", "vector", "0")
    scan = sh(1, "upload/x/scan.png", "SEN-DFE-XX-00-D-X-0201", "Special Schoo! Plan Leve1 00", "scan", "0")
    P.find_copies([pdf, scan])
    assert scan.get("copy_of") == pdf["id"] and "drawing number" in scan["copy_how"]


# ------------------------------------------------------------------ several buildings in one upload (T3 cycle 4)
def _sh(name, number, title, storey, kind="floor_plan", medium="vector", partial=False):
    f = f"upload/{UID}/{name}"
    return {"id": f"{f}#0", "file": f, "page": 0, "number": number, "title": title, "kind": kind,
            "role": P.ROLE_NAMES.get(kind, kind), "kind_source": "title block", "storey": storey,
            "level": P.level_name(storey) if storey is not None else "", "level_source": "title block",
            "building": "", "partial": partial, "ratio": None, "page_ratio": None, "confidence": 0.8, "user": {},
            "medium": medium}


def _two_schools():
    return [_sh("sen0.pdf", "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", "0"),
            _sh("sen1.pdf", "SEN-DFE-XX-01-D-X-0202", "Special School Plan Level 01", "1"),
            _sh("pri0.pdf", "PRI-DFE-XX-00-D-X-0201", "Primary School GA Plan Level 00", "0"),
            _sh("pri1.pdf", "PRI-DFE-XX-01-D-X-0202", "Primary School GA Plan Level 01", "1"),
            _sh("land.pdf", "", "Primary School Landscape Master Plan", None, kind="site_plan"),
            _sh("other.pdf", "ZZQ-ABC-XX-XX-D-L-0001", "Landscape Masterplan", None, kind="site_plan"),
            _sh("scan.png", "", "", None, kind="unknown", medium="image")]


def test_two_buildings_in_one_upload_are_split_by_drawing_number():
    sheets = _two_schools()
    sheets[-1]["copy_of"] = sheets[0]["id"]  # a scan of SEN Level 00
    names = P.split_buildings(sheets)
    assert names == ["Primary School", "Special School"]
    b = {s["file"].split("/")[-1]: s["building"] for s in sheets}
    assert b["sen0.pdf"] == b["sen1.pdf"] == "Special School" and b["pri0.pdf"] == b["pri1.pdf"] == "Primary School"
    assert b["land.pdf"] == "Primary School"  # no number: its title names the primary school
    assert b["other.pdf"] == ""  # a code with no floor plan is not a building of its own
    assert b["scan.png"] == "Special School"  # a copy goes with its original
    assert P._guess_name(sheets, []) in ("Special School + Primary School", "Primary School + Special School")
    levels = P.storeys(sheets)
    assert {(lv["building"], lv["storey"]) for lv in levels} >= {("Special School", "0"), ("Primary School", "0")}


def test_one_building_is_never_split():
    sheets = _two_schools()[:2] + [_sh("x.pdf", "SEN-DFE-XX-XX-D-L-0001", "Special School Landscape", None, kind="site_plan")]
    assert P.split_buildings(sheets) == [] and all(s["building"] == "" for s in sheets)
    # US set: 'AREA A' / 'AREA B' of one level are parts of one building, not two
    us = [_sh("a.pdf", "A-101", "FIRST FLOOR PLAN - AREA A", "1"), _sh("b.pdf", "A-102", "FIRST FLOOR PLAN - AREA B", "1"),
          _sh("c.pdf", "A-103", "SECOND FLOOR PLAN", "2")]
    assert P.split_buildings(us) == []
    # titles that differ but never draw one level twice: not split on a title word alone
    t = [_sh("a.pdf", "", "Classroom Block Level 00", "0"), _sh("b.pdf", "", "Sports Hall Level 01", "1")]
    assert P.split_buildings(t) == []
    t.append(_sh("c.pdf", "", "Sports Hall Level 00", "0"))  # now Level 00 has two whole plans …
    assert P.split_buildings(t) == []  # … but a building drawn by its titles needs plans of 2+ levels (USACE fuel set)
    t.append(_sh("d.pdf", "", "Classroom Block Level 01", "1"))
    assert P.split_buildings(t) == ["Classroom Block", "Sports Hall"]


def test_building_scope_follows_the_question_then_the_sheet_on_screen():
    sheets = _two_schools()[:4]
    P.split_buildings(sheets)
    proj = {"id": UID, "sheets": sheets, "items": []}
    sen0, pri0 = sheets[0]["id"], sheets[2]["id"]
    q = "How many doors are there in the whole building?"
    pl = P.plan(proj, q, pri0)
    assert pl["building"] == "Primary School" and [s["building"] for s in pl["sheets"]] == ["Primary School"] * 2
    assert sum("another building (Special School)" in s["why"] for s in pl["skipped"]) == 2
    a = P.combine(proj, q, _parts(pl, [10, 11]), pri0)
    assert a["value"] == 21 and a["building"] == "Primary School" and "Primary School only" in a["assumptions"][0]
    assert P.plan(proj, "How many doors does the special school have?", pri0)["building"] == "Special School"
    both = "How many doors are there in both schools together?"
    pl = P.plan(proj, both, sen0)
    assert pl["building"] == "" and len(pl["sheets"]) == 4
    a = P.combine(proj, both, _parts(pl, [10, 11, 12, 13]), sen0)
    assert a["value"] == 46 and {b["building"]: b["value"] for b in a["per_building"]} == {"Special School": 21,
                                                                                           "Primary School": 25}
    assert all(" · Level 0" in r["level"] for r in a["per_sheet"])
    # lifts: the most on one level of EACH building, added (never 10 + 11 + 12 + 13)
    lifts = "How many lifts are there in all buildings?"
    a = P.combine(proj, lifts, _parts(P.plan(proj, lifts, sen0), [1, 2, 1, 3]), sen0)
    assert a["value"] == 5
    # 'Level 01' while a primary sheet is open = the primary school's Level 01
    r = P.route(proj, "How many doors are on Level 01?", pri0)
    assert r is not None and r["id"] == sheets[3]["id"]


def test_user_names_the_building_of_a_sheet(up):
    proj = P._open(up[:2])
    assert P.buildings(proj["sheets"]) == []
    sh = proj["sheets"][1]
    sh["user"] = {"building": "Annex"}
    proj["sheets"][0]["user"] = {"building": "Main house"}
    assert P.buildings(proj["sheets"]) == ["Main house", "Annex"]
    assert P.plan(proj, "How many doors are there in the whole building?", sh["id"])["building"] == "Annex"


def test_a_project_stored_by_an_older_reader_is_read_again(up):
    proj = P._open(up[:2])
    proj["version"] = 1
    for s in proj["sheets"]:
        s["building"] = "stale"
    P.save(UID, proj)
    again = P._open(up[:2])
    assert again["version"] == P.VERSION and all(s["building"] == "" for s in again["sheets"])


def test_two_cad_sheets_one_letter_apart_are_two_drawings():
    # 'SEC-DFE-XX-00-…' (secondary school) vs 'SEN-DFE-XX-00-…' (special school): both CAD, so no OCR slip to forgive
    sheets = [_sh("sen0.pdf", "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", "0"),
              _sh("sec0.pdf", "SEC-DFE-XX-00-D-X-0201", "Secondary School GA Plan Level 00", "0")]
    P.find_copies(sheets)
    assert not any(s.get("copy_of") for s in sheets)
    # a scan whose OCR read one letter wrong is still a copy, when its title agrees
    scan = _sh("scan.png", "SEN-DFE-XX-0O-D-X-0201", "Special Schoo! Plan Level 00", "0", medium="image")
    P.find_copies(sheets + [scan])
    assert scan.get("copy_of") == sheets[0]["id"]
    # … and a scan of the OTHER school's sheet (one letter apart from this one) is not, as its title is another's
    scan2 = _sh("scan2.png", "SEC-DFE-XX-0O-D-X-0201", "Secondary Schoo1 GA Plan Level 00", "0", medium="image")
    P.find_copies([sheets[0], scan2])
    assert not scan2.get("copy_of")


def test_two_cad_prints_of_one_plan_are_one_sheet(tmp_path):
    rooms = [(100, 100, 200, 150), (300, 100, 150, 150), (100, 250, 350, 120)]
    _plan_pdf(tmp_path / "a.pdf", rooms, title="Secondary School Second Floor Plan")
    _plan_pdf(tmp_path / "a.paperA1.pdf", rooms, title="Secondary School Second Floor Plan")
    _plan_pdf(tmp_path / "b.pdf", [(80, 300, 500, 200), (600, 60, 120, 100)], title="Secondary School Second Floor Plan")
    sheets, abs_of = [], {}
    for n in ("a.pdf", "a.paperA1.pdf", "b.pdf"):
        s = _sh(n, "", "Secondary School Second Floor Plan", "2")
        sheets.append(s)
        abs_of[s["id"]] = str(tmp_path / n)
    P.find_copies(sheets, abs_of)
    assert sheets[1].get("copy_of") == sheets[0]["id"] and "alike" in sheets[1]["copy_how"]
    assert not sheets[2].get("copy_of")  # same title, another drawing: two plans of one level (asked 'which one?')


def test_a_number_on_four_scans_is_the_sets_number():
    sheets = [_sh(f"c{i}.png", "HAER No. HI-76", "", None, kind="unknown", medium="image") for i in range(4)]
    P.find_copies(sheets)
    assert not any(s.get("copy_of") for s in sheets)


def test_finnish_german_swedish_level_words():
    L = P.level_from_name
    # Finnish counts the entrance floor as '1. kerros' (VP's stoa.sheet.sets: '1. KERROS' = storey 0)
    assert L("POHJAPIIRUSTUS_1._KERROS.x")[0] == "0" and L("POHJAPIIRUSTUS_2._krs.x")[0] == "1"
    assert L("KELLARIKERROS.x")[0] == "-1" and L("VESIKATTO.x")[0] == "roof" and L("ULLAKKO.x")[0] == "attic"
    assert L("POHJAPIIRUSTUS_1._JA_2._KERROS.x") == (None, None) and L("1.-2._krs.x") == (None, None)
    assert L("Grundriss_Obergeschoss_2.x")[0] == "2" and L("Grundriss_Untergeschoss.x")[0] == "-1"
    assert L("plan_2._våningen.x")[0] == "1" and L("källare.x")[0] == "-1"
    assert P.level_name("attic") == "Attic"


def test_level_read_from_the_title_when_the_title_block_reader_has_none(up, monkeypatch):
    from types import SimpleNamespace

    import stoa.sheet.sets as sets

    monkeypatch.setattr(sets, "classify", lambda path, i=0, layer=None: SimpleNamespace(
        page=i, number="ARK-102", title="POHJAPIIRUSTUS 2. KRS", kind="floor_plan", storey=None, storey_name=None,
        building=None, partial=False, ratio=None, page_ratio=None, confidence=0.6))
    sh = P.classify_file(up[0])[0]
    assert sh["storey"] == "1" and sh["level"].lower() == "2 krs" and sh["level_source"] == "title block"


def test_finnish_houses_a_and_b_are_two_buildings():
    t = P._title_key
    assert t("POHJAPIIRUSTUS 1. KERROS TALO A") == "talo A" and t("BUILDING 3305 FIRST FLOOR PLAN") == "building 3305"
    assert t("Wing A Level 01 Plan") == "" and t("building plan") == "building"
    sheets = [_sh("a1.pdf", "", "POHJAPIIRUSTUS 1. KERROS TALO A", "0"), _sh("a2.pdf", "", "POHJAPIIRUSTUS 2. KERROS TALO A", "1"),
              _sh("b1.pdf", "", "POHJAPIIRUSTUS 1. KERROS TALO B", "0"), _sh("b2.pdf", "", "POHJAPIIRUSTUS 2. KERROS TALO B", "1")]
    assert P.split_buildings(sheets) == ["Talo A", "Talo B"]


def test_a_single_letter_is_not_a_building_name_in_a_question():
    sheets = [_sh("a1.pdf", "", "POHJAPIIRUSTUS 1. KERROS TALO A", "0"), _sh("a2.pdf", "", "POHJAPIIRUSTUS 2. KERROS TALO A", "1"),
              _sh("b1.pdf", "", "POHJAPIIRUSTUS 1. KERROS TALO B", "0"), _sh("b2.pdf", "", "POHJAPIIRUSTUS 2. KERROS TALO B", "1")]
    P.split_buildings(sheets)
    proj = {"id": UID, "sheets": sheets, "items": []}
    b1 = sheets[2]["id"]
    assert P.target_building(proj, "How big is a classroom in the whole building?", b1)[0] == "Talo B"  # the sheet's
    assert P.target_building(proj, "How many doors are there in talo A?", b1)[0] == "Talo A"


def test_a_word_of_the_sheet_titles_is_not_a_room(monkeypatch):
    sheets = [_sh("sen0.pdf", "", "Special School Plan Level 00", "0"), _sh("sen1.pdf", "", "Special School Plan Level 01", "1")]
    txt = {sheets[0]["id"]: P._norm_text("Special School Plan Level 00 Multi-Purpose Hall Classroom Kitchen"),
           sheets[1]["id"]: P._norm_text("Special School Plan Level 01 Classroom Staff Room")}
    monkeypatch.setattr(P, "_sheet_text", lambda sh: txt[sh["id"]])
    assert P.named_room("How many classrooms does the special school have?", sheets) is None
    r = P.named_room("How many doors does the Multi-Purpose Hall have?", sheets)
    assert r is not None and r["on"] == [sheets[0]["id"]]


def test_two_pictures_of_one_sheet_are_one_sheet(tmp_path):
    from PIL import Image

    _plan_pdf(tmp_path / "p.pdf", [(100, 100, 200, 150), (300, 100, 150, 150), (100, 250, 350, 120)])
    _plan_pdf(tmp_path / "q.pdf", [(80, 300, 500, 200), (600, 60, 120, 100)])
    import pymupdf

    for n in ("p", "q"):
        with pymupdf.open(tmp_path / f"{n}.pdf") as d:
            d[0].get_pixmap(dpi=60).save(tmp_path / f"{n}.png")
    Image.open(tmp_path / "p.png").convert("L").save(tmp_path / "p_tif.png")  # the same scan, another format
    sheets, abs_of = [], {}
    for n in ("p.png", "q.png", "p_tif.png"):
        s = _sh(n, "", "", None, kind="unknown", medium="image")
        sheets.append(s)
        abs_of[s["id"]] = str(tmp_path / n)
    P.find_copies(sheets, abs_of)
    assert sheets[2].get("copy_of") == sheets[0]["id"] and not sheets[0].get("copy_of") and not sheets[1].get("copy_of")


def test_which_one_reads_notes_that_start_with_the_planners_sub_question():
    q = "What is the area of the Secondary Classroom in the drawing? — "
    ans = {"value": 51.45, "unit": "m2", "confidence": 0.45, "solver": "pipeline/plan_rooms",
           "assumptions": ["2 rooms are labelled 'Secondary Classroom' and measure alike (52.13, 50.77): their mean"],
           "evidence": [{"file": "f.pdf", "page": 0, "bbox": [1, 2, 3, 4], "note": q + "Secondary Classroom: 52.13 m² traced at 1:200"},
                        {"file": "f.pdf", "page": 0, "bbox": [5, 6, 7, 8], "note": q + "Secondary Classroom: 50.77 m² traced at 1:200"}]}
    it = P._item_from("How big is the classroom?", ans, "sheet", "f.pdf", 0, "L00", "Level 00")
    assert [c["value"] for c in it["ambiguous"]["choices"]] == [52.13, 50.77]
    assert it["ambiguous"]["choices"][0]["label"] == "Secondary Classroom"


def _block_pdf(path, rooms, block_lines):
    """A plan with a title block in the right strip (x > 80 % of the page)."""
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    for x, y, w, h in rooms:
        pg.draw_rect(pymupdf.Rect(x, y, x + w, y + h), color=(0, 0, 0), width=2)
    for i, t in enumerate(block_lines):
        pg.insert_text((700, 420 + 14 * i), t, fontsize=8)
    d.save(str(path))
    d.close()


def test_two_sets_with_plain_titles_are_split_by_their_title_blocks(tmp_path):
    firm = "Arkkitehdit Virta Oy"
    sets = {"a": ["As Oy Kotiranta", "Rantatie 5, Espoo", firm], "b": ["As Oy Metsapuisto", "Puistokatu 12, Vantaa", firm]}
    sheets, abs_of = [], {}
    for k, lines in sets.items():
        for st, title in (("0", "GROUND FLOOR PLAN"), ("1", "FIRST FLOOR PLAN")):
            n = f"{k}{st}.pdf"
            _block_pdf(tmp_path / n, [(60, 60, 300, 200 + 40 * int(st))], lines + [title])
            s = _sh(n, "", title, st)
            sheets.append(s)
            abs_of[s["id"]] = str(tmp_path / n)
    P.find_copies(sheets, abs_of)
    names = P.split_buildings(sheets, abs_of)
    assert names == ["As Oy Kotiranta", "As Oy Metsapuisto"], names
    assert [s["building"] for s in sheets] == ["As Oy Kotiranta"] * 2 + ["As Oy Metsapuisto"] * 2
    assert all(s["building_source"] == "title block" for s in sheets)
    # one set only (no level drawn twice): never split, whatever its title block says
    one = sheets[:2]
    for s in one:
        s["building"] = ""
    assert P.split_buildings(one, abs_of) == []


def test_naming_one_sheets_building_splits_off_the_rest(up):
    proj = P._open(up[:2])
    a, b = proj["sheets"][0], proj["sheets"][1]
    b["user"] = {"building": "Annex"}
    assert P.buildings(proj["sheets"]) == ["Other sheets", "Annex"]
    pl = P.plan(proj, "How many doors are there in the whole building?", b["id"])
    assert pl["building"] == "Annex" and [s["id"] for s in pl["sheets"]] == [b["id"]]
    assert {lv["building"] for lv in P.view(proj)["levels"]} == {"Annex", "Other sheets"}


def test_a_building_the_upload_does_not_have_is_said():
    sheets = _two_schools()[:4]
    for s in sheets[2:]:  # make the second school the secondary one
        s["number"] = s["number"].replace("PRI", "SEC")
        s["title"] = s["title"].replace("Primary", "Secondary")
    P.split_buildings(sheets)
    proj = {"id": UID, "sheets": sheets, "items": []}
    t, why = P.target_building(proj, "How many classrooms does the primary school have?", sheets[0]["id"])
    assert t == "Special School" and "no building in this upload is 'primary school'" in why
    a = P.combine(proj, "How many classrooms does the primary school have?",
                  _parts(P.plan(proj, "How many classrooms does the primary school have?", sheets[0]["id"]), [8, 3]),
                  sheets[0]["id"])
    assert a["value"] == 11 and a["assumptions"][0].startswith("no building in this upload is 'primary school'")
    assert P.target_building(proj, "How many doors does the whole school have?", sheets[0]["id"])[1].startswith("Special School:")


def test_estonian_level_words_and_a_plan_line_of_the_sheet_text(tmp_path, monkeypatch):
    L = P.level_from_name
    assert L("plan-03-3-korrus.x")[0] == "2" and L("Korruse_plaan_1.korrus.x")[0] == "0" and L("korrus_2.x")[0] == "1"
    assert L("keldrikorrus.x")[0] == "-1" and L("katusekorrus.x")[0] == "attic" and L("katus.x")[0] == "roof"
    T = P._level_from_text
    assert T("KORIDOR\n119\nMaleva tänav 18 korterelamu — Korruse plaan: 3.korrus\nMõõtkava 1:100")[0] == "2"
    assert T("GROUND FLOOR PLAN\nSTORE\n12")[0] == "0"
    # a cross-reference, a sheet of two levels, or two lines naming different levels: no level
    assert T("SEE FIRST FLOOR PLAN FOR CONTINUATION") == (None, None)
    assert T("GROUND AND FIRST FLOOR PLANS") == (None, None)
    assert T("GROUND FLOOR PLAN\nFIRST FLOOR PLAN") == (None, None)
    assert T("Level 2 store\nOffice 12") == (None, None)  # no plan word: a room label is not the sheet's level
    # a renamed upload ('sheet3.pdf') whose title block says nothing: the plan line names its level and kind
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    _pdf(root / UID / "sheet3.pdf", "Maleva 18 - Korruse plaan: 3.korrus")
    sh = P.classify_file(f"upload/{UID}/sheet3.pdf")[0]
    assert sh["storey"] == "2" and sh["level_source"] == "sheet text" and sh["kind"] == "floor_plan"


def test_a_building_total_over_levels_says_whole_building_in_its_intent(up):
    proj = P._open(up[:3])
    q = "How many doors are there in the whole building?"
    pl = P.plan(proj, q)
    parts = _parts(pl, [12, 9])
    for p in parts:
        p["answer"]["intent"] = {"task": "count", "target": "door", "scope": "sheet", "filters": {"storey": "1"}}
    a = P.combine(proj, q, parts)
    assert a["intent"]["scope"] == "model" and a["intent"]["target"] == "door" and "storey" not in a["intent"]["filters"]
    one = P.combine(proj, q, parts[:1])  # one level only: the sheet's own intent stays
    assert one["intent"]["scope"] == "sheet"


def test_untitled_sheets_take_their_plan_line_and_the_shared_part_names_the_building(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_PROJECTS_DIR", str(tmp_path / "projects"))
    for i in (1, 2, 3):
        _pdf(root / UID / f"sheet{i}.pdf", f"Maleva 18 korterelamu (example) - Korruse plaan: {i}.korrus")
    proj = P.build([f"upload/{UID}/sheet{i}.pdf" for i in (1, 2, 3)])
    assert proj["name"] == "Maleva 18 korterelamu"
    sh = P.effective_all(proj["sheets"])
    assert [s["storey"] for s in sh] == ["0", "1", "2"] and sh[2]["title"] == "Korruse plaan: 3.korrus"
    assert not any(s.get("copy_of") for s in sh) and len(P.buildings(sh)) <= 1


def test_notes_and_tags_in_the_sheet_text_name_no_level():
    T = P._level_from_text  # stoa-like sheets the loose rule got wrong (FTC p15 / p25, AW 078-24-33 p20)
    assert T("ROOF DECK PER PLAN\nBEAM PER PLAN, TYP") == (None, None)
    assert T("PLAN\nB2 BRIDGE CRANE PLAN") == (None, None)
    assert T("STEEL TANK LAYOUT\nSTEEL TANK ROOF LAYOUT\nSTEEL TANK FLOOR LAYOUT") == (None, None)
    assert T("BASEMENT PLAN")[0] == "-1" and T("Grundriss Erdgeschoss")[0] == "0"


def test_a_floor_number_follows_the_printed_level_names_counted_from_one(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / UID).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_PROJECTS_DIR", str(tmp_path / "projects"))
    for i in (1, 2, 3, 4):
        _pdf(root / UID / f"sheet{i}.pdf", f"Maleva 18 - Korruse plaan: {i}.korrus")
    proj = P._open([f"upload/{UID}/sheet{i}.pdf" for i in (1, 2, 3, 4)])
    ids = {s["storey"]: s["id"] for s in proj["sheets"]}
    r = P.route(proj, "How many doors are on the 3rd floor?", ids["0"])
    assert r["id"] == ids["2"] and "3 korrus" in r["why"] and r["question"] == "How many doors are on this plan?"
    assert P.route(proj, "How many doors are on floor 4?", ids["0"])["id"] == ids["3"]
    assert P.route(proj, "How many doors are on the ground floor?", ids["2"])["id"] == ids["0"]
    assert P._by_printed_number(proj["sheets"] + [dict(proj["sheets"][0], storey="-1", level="Kelder")],
                                "doors on the 3rd floor", "3")[0] == "2"  # a basement without a number changes nothing
    assert P.level_free("What is the area of the first floor landing?") == "What is the area of the first floor landing?"


def test_floor_area_answers_say_what_they_include():
    g = P._scope_of_area({"value": 1051.5, "unit": "m2", "solver": "project/gross_area", "trace": ["x"]})
    assert g["trace"][0] == "convention: gross internal area (GIA)" and "the outer walls" in g["excludes"]
    r = P._scope_of_area({"value": 900.0, "unit": "m2", "solver": "project/rooms_sum"})
    assert r["trace"][0].startswith("convention: net") and r["includes"] == ["the traced rooms"]
    assert "includes" not in P._scope_of_area({"value": None, "solver": "project/gross_area"})


def test_a_floor_of_the_other_building_leaves_the_sheet_on_screen():
    sh = [{"id": f"upload/{UID}/{b}{st}.pdf#0", "file": f"upload/{UID}/{b}{st}.pdf", "page": 0, "storey": st, "level": f"Level 0{st}",
           "kind": "floor_plan", "building": name, "partial": False, "label": f"{name} {st}", "user": {}}
          for b, name in (("pri", "Primary School"), ("sen", "Special School")) for st in ("0", "1")]
    proj = {"sheets": sh}
    r = P.route(proj, "How many doors are on the ground floor of the special school?", f"upload/{UID}/pri0.pdf#0")
    assert r["file"].endswith("sen0.pdf") and r["here"] is False and r["question"] == "How many doors are on this plan?"
    here = P.route(proj, "How many doors are on the ground floor?", f"upload/{UID}/pri0.pdf#0")
    assert here["here"] is True and here["file"].endswith("pri0.pdf")


def test_the_sheets_printed_level_wins_over_a_typed_file_name(tmp_path, monkeypatch):
    # 'floor1.pdf' of a sheet printing 'Korruse plaan: 1.korrus' is the ground floor (Estonian 1.korrus = storey 0),
    # not storey 1; an ISO 19650 level field is the office's own code and still comes first; a typed name is used
    # when the sheet prints no level of its own (T3 cycle 6)
    root = tmp_path / "uploads"
    for d in (UID, "b" * 12, "c" * 12):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    _pdf(root / UID / "floor1.pdf", "Maleva 18 - Korruse plaan: 1.korrus")
    sh = P.classify_file(f"upload/{UID}/floor1.pdf")[0]
    assert sh["storey"] == "0" and sh["level_source"] == "sheet text"
    _pdf(root / ("b" * 12) / "SEN-DFE-XX-01-D-X-0202.pdf", "Maleva 18 - Korruse plaan: 1.korrus")
    sh = P.classify_file(f"upload/{'b' * 12}/SEN-DFE-XX-01-D-X-0202.pdf")[0]
    assert sh["storey"] == "1" and sh["level_source"] == "file name"
    _pdf(root / ("c" * 12) / "floor2.pdf", "STORE\nOFFICE 12")
    sh = P.classify_file(f"upload/{'c' * 12}/floor2.pdf")[0]
    assert sh["storey"] == "2" and sh["level_source"] == "file name"


def test_a_total_over_two_buildings_says_both_buildings():
    sheets = [{"id": f"upload/{UID}/{b}{n}.pdf#0", "file": f"upload/{UID}/{b}{n}.pdf", "page": 0, "storey": str(n),
               "level": f"Level 0{n}", "building": name, "kind": "floor_plan", "role": "Floor plan", "title": "",
               "number": "", "partial": False, "user": {}, "label": f"{name} L0{n}"}
              for b, name in (("s", "Special School"), ("p", "Primary School")) for n in (0, 1)]
    proj = {"id": UID, "sheets": sheets, "items": []}
    q = "How many doors are there in both schools together?"
    pl = P.plan(proj, q, sheets[0]["id"])
    a = P.combine(proj, q, _parts(pl, [10, 11, 12, 13]), sheets[0]["id"])
    assert a["value"] == 46 and a["where"] == "in both buildings"
    one = P.combine(proj, "How many doors are there in the whole building?",
                    _parts(P.plan(proj, "How many doors are there in the whole building?", sheets[0]["id"]), [10, 11]),
                    sheets[0]["id"])
    assert "where" not in one


def _set_sheets(rows):
    """FTC-like full US set: (page, number, title, kind, storey)."""
    return [{"id": f"upload/{UID}/set.pdf#{p}", "file": f"upload/{UID}/set.pdf", "page": p, "number": n, "title": t,
             "kind": k, "role": P.ROLE_NAMES.get(k, k), "storey": st, "level": {"1": "First floor", "2": "Second floor"}.get(st, ""),
             "building": "", "partial": False, "user": {}, "label": f"{n} · {t}", "ratio": 96.0, "page_ratio": 96.0,
             "plan_line": "", "kind_source": "title block", "level_source": "title block" if st else ""}
            for p, n, t, k, st in rows]


FTC = [(0, "", "", "unknown", None), (33, "A-101", "LIFE SAFETY PLAN - FIRST FLOOR", "floor_plan", "1"),
       (34, "A-102", "LIFE SAFETY PLAN - SECOND FLOOR", "floor_plan", "2"),
       (35, "A-111", "FLOOR PLAN - FIRST FLOOR", "floor_plan", "1"), (36, "A-112", "FLOOR PLAN - SECOND FLOOR", "floor_plan", "2"),
       (37, "A-121", "REFLECTED CEILING PLAN - FIRST FLOOR", "rcp", "1"),
       (38, "A-122", "REFLECTED CEILING PLAN - SECOND FLOOR", "rcp", "2"),
       (49, "A-410", "D7 STAIR 1 - SECOND FLOOR", "floor_plan", "2"),
       (61, "A-701", "FURNITURE PLAN - FIRST FLOOR", "floor_plan", "1"),
       (94, "FA-101", "FIRE ALARM AND MASS NOTIFICATION - FIRST FLOOR", "floor_plan", "1"),
       (95, "FA-102", "FIRE ALARM AND MASS NOTIFICATION - SECOND FLOOR", "floor_plan", "2"),
       (88, "P-501", "PLUMBING DETAILS", "detail", "0")]


def test_a_full_set_asks_one_plan_per_level():
    # T3 cycle 6: FTC (125 pages) drew each floor as a floor plan + life safety + ceiling + furniture + trade plans;
    # a building door count must add A-111 + A-112 only, a ceiling question the two RCPs
    sheets = _set_sheets(FTC)
    proj = {"id": UID, "sheets": sheets, "items": []}
    assert P.split_buildings(sheets) == []  # 'LIFE SAFETY' / 'FIRE ALARM …' titles are no buildings
    pl = P.plan(proj, "How many doors are there in the whole building?")
    assert [s["label"][:5] for s in pl["sheets"]] == ["A-111", "A-112"]
    why = {s["label"][:6]: s["why"] for s in pl["skipped"]}
    assert "one plan per level" in why["A-101 "] and "one plan per level" in why["FA-101"]
    pl = P.plan(proj, "How many light fixtures are there in the whole building?")
    assert [s["label"][:5] for s in pl["sheets"]] == ["A-121", "A-122"]
    cur = sheets[3]["id"]
    assert P.route(proj, "How many doors are on the second floor?", cur)["label"].startswith("A-112")
    assert P.route(proj, "How many fire extinguishers are on the second floor?", cur)["label"].startswith("A-102")
    assert P.route(proj, "How many light fixtures are on the second floor?", cur)["label"].startswith("A-122")
    # a detail sheet printing 'ground floor' makes no level of its own
    assert [(r["storey"], r["level"]) for r in P.storeys(sheets)] == [("1", "First floor"), ("2", "Second floor"),
                                                                       (None, "No level read")]


def test_purpose_of_a_plan():
    pu = P._purpose
    assert pu({"title": "LIFE SAFETY PLAN - FIRST FLOOR"}) == "life safety plan"
    assert pu({"title": "FLOOR PLAN & RCP"}) == "" and pu({"title": "GROUP EXERCISE - FLOOR PLAN & RCP"}) == ""
    assert pu({"title": "FIRE SUPPRESSION - FIRST FLOOR", "number": "FP-101"}) == "services plan"
    assert pu({"title": "FLOOR PLAN - FIRST FLOOR", "number": "A-111"}) == ""
    assert pu({"title": "Primary School GA Plan Level 00", "number": "PRI-DFE-XX-00-D-X-0201"}) == ""
    assert pu({"title": "Korruse plaan: 3.korrus"}) == ""


def test_a_building_area_uses_the_total_printed_in_the_set(monkeypatch):
    # FTC: the life safety plans print 'TOTAL GROSS FLOOR AREA = 30,125 GSF'; the traced levels add up to 3,744 m²
    # (+34 %): the building answer is the printed figure, marked on its sheet, the traced sum its cross-check
    F = pytest.importorskip("stoa.sheet.footprint")  # VP's (d31+); the module, not stoa.sheet's function of that name
    sheets = _set_sheets(FTC)
    for s in sheets:
        s["medium"] = "vector"
    proj = {"id": UID, "sheets": sheets, "items": []}
    monkeypatch.setattr(P, "resolve", lambda f: __file__)
    monkeypatch.setattr(F, "printed_gross", lambda p, page=0, layer=None: [
        {"value": 30125.0, "unit": "ft2", "kind": "total", "bbox": [1, 2, 3, 4],
         "text": "TOTAL GROSS FLOOR AREA: 30,125 GSF"}] if page in (33, 34) else [])
    P._PT_CACHE.clear()
    q = "What is the total floor area of the building?"
    pl = P.plan(proj, q)
    parts = [{"sheet": s["id"], "answer": {"value": v, "unit": "m2", "confidence": 0.3, "evidence": [],
                                           "solver": "project/gross_area"}} for s, v in zip(pl["sheets"], [2157.1, 1587.0])]
    a = P.combine(proj, q, parts)
    assert a["value"] == 2798.7 and a["solver"] == "project/printed_total" and a["evidence"][0]["page"] == 33
    assert "+34 %" in a["assumptions"][1] and "same figure on 2 sheets" in a["assumptions"][0]
    # net / usable questions keep the traced rooms; two different printed totals give no single figure
    assert P.combine(proj, "What is the total net floor area of the building?", parts)["solver"] != "project/printed_total"
    monkeypatch.setattr(F, "printed_gross", lambda p, page=0, layer=None: [
        {"value": 30125.0 + 1000 * page, "unit": "ft2", "kind": "total", "bbox": None, "text": "TOTAL"}] if page in (33, 34) else [])
    P._PT_CACHE.clear()
    assert P.combine(proj, q, parts)["solver"] != "project/printed_total"
    P._PT_CACHE.clear()


def test_a_set_with_no_levels_read_is_summed_as_a_best_estimate():
    # pictures before their text layer is read (or plans printing no level): a sum with its assumption, capped, never
    # a refusal; one levelled sheet among them keeps the old 'name their levels' answer
    sheets = [{"id": f"upload/{UID}/p{n}.png#0", "file": f"upload/{UID}/p{n}.png", "page": 0, "storey": None, "level": "",
               "building": "", "kind": "drawing", "role": "Drawing", "title": "", "number": "", "partial": False,
               "user": {}, "label": f"p{n}.png", "kind_source": "not read", "plan_line": ""} for n in range(3)]
    proj = {"id": UID, "sheets": sheets, "items": []}
    q = "How many doors are there in the whole building?"
    pl = P.plan(proj, q)
    assert len(pl["sheets"]) == 3
    a = P.combine(proj, q, _parts(pl, [10, 12, 9]))
    assert a["value"] == 31 and a["confidence"] <= 0.35 and "each is taken as a different floor" in a["assumptions"][0]


def test_a_named_building_is_asked_on_each_sheet_as_this_plan():
    # three schools in one drop: 'How many doors does the secondary school have?' was asked verbatim on each sheet and
    # 'secondary' matched the 'Secondary Classroom' labels (≈ 21, a rough guess); 'total floor area of the secondary
    # school' was not a floor-area question (150 m², one room)
    names = ["Primary School", "Secondary School", "Special School"]
    bn = P.building_neutral
    assert P.sheet_question(bn("How many doors does the secondary school have?", names)) == "How many doors are there on this plan?"
    assert P.is_floor_area_question(bn("What is the total floor area of the secondary school?", names))
    assert bn("How many secondary classrooms are there?", names) == "How many secondary classrooms are there?"


def test_a_building_door_count_uses_the_sets_door_schedule(tmp_path, monkeypatch):
    # FTC: A-610's DOOR SCHEDULE lists 66 marks ('100.A' …) and the plans print 43 + 22 of them next to their doors,
    # while the door symbols add up to 68 + 23 = 91: the building answer is the schedule's count, each level's row its
    # tagged doors (each tag marked), the symbol sum the cross-check; a kind of door ('fire doors') is left alone
    import pymupdf

    pdf = tmp_path / "set.pdf"
    doc = pymupdf.open()
    plan = doc.new_page(width=800, height=600)
    plan.insert_text((40, 40), "FLOOR PLAN - FIRST FLOOR")
    for i in range(10):  # 10 of the 12 scheduled doors tagged on the plan, plus a room number
        plan.insert_text((60 + 60 * i, 200), f"1{i:02d}A")
    plan.insert_text((60, 300), "105")
    sched = doc.new_page(width=800, height=600)
    sched.insert_text((40, 40), "DOOR SCHEDULE")
    sched.insert_text((40, 60), "NUMBER")
    for i in range(12):
        sched.insert_text((40, 80 + 20 * i), f"1{i:02d}A")
    doc.save(pdf)
    monkeypatch.setattr(P, "resolve", lambda f: str(pdf))
    base = {"building": "", "partial": False, "user": {}, "plan_line": "", "number": "", "medium": "vector"}
    sheets = [base | {"id": f"upload/{UID}/set.pdf#0", "file": f"upload/{UID}/set.pdf", "page": 0, "storey": "1",
                      "level": "First floor", "kind": "floor_plan", "role": "Floor plan", "title": "FLOOR PLAN - FIRST FLOOR",
                      "label": "A-111", "kind_source": "title block"},
              base | {"id": f"upload/{UID}/set.pdf#1", "file": f"upload/{UID}/set.pdf", "page": 1, "storey": None,
                      "level": "", "kind": "schedule", "role": "Schedule", "title": "DOOR SCHEDULE", "label": "A-610",
                      "kind_source": "sheet title"}]
    proj = {"id": UID, "sheets": sheets, "items": []}
    parts = [{"sheet": sheets[0]["id"], "answer": {"value": 15, "unit": "count", "confidence": 0.6, "evidence": [],
                                                   "solver": "openings"}}]
    P._DS_CACHE.clear()
    a = P.combine(proj, "How many doors does the building have?", parts)
    assert a["value"] == 12 and a["solver"] == "project/door_schedule" and a["confidence"] == 0.75
    assert "DOOR SCHEDULE: 12 doors (100A … 111A)" in a["assumptions"][0] and "tag 10 of them" in a["assumptions"][1]
    assert "not tagged on these plans: 110A, 111A" in a["assumptions"][1]
    assert "add up to 15" in a["assumptions"][1]
    row = a["per_sheet"][0]
    assert row["value"] == 10 and "door symbols seen: 15" in row["note"]
    tags = [e for e in a["evidence"] if e["page"] == 0]
    assert len(tags) == 10 and all(e["bbox_space"] == "pdf_pt" for e in tags)
    assert P.combine(proj, "How many fire doors are there?", parts)["solver"] != "project/door_schedule"
    # a door count asked on one level: the plan's own count, with its schedule doors as the cross-check line
    r = P.route(proj, "How many doors are on the first floor?")
    t = r["tags"]
    assert r["id"] == sheets[0]["id"] and t["n"] == 10 and t["total"] == 12 and len(t["evidence"]) == 10
    assert "10 doors of the door schedule" in t["note"] and "(12 doors in all)" in t["note"]
    assert P.route(proj, "How many windows are on the first floor?")["tags"] is None
    # the same on the sheet on screen with no level named; the schedule sheet itself / other questions: nothing
    h = P.route(proj, "How many doors are on this plan?", sheets[0]["id"])
    assert h["here"] is True and h["tags"]["n"] == 10 and h["question"] == "How many doors are on this plan?"
    assert P.route(proj, "How many doors are on this plan?", sheets[1]["id"]) is None
    assert P.route(proj, "How many rooms are on this plan?", sheets[0]["id"]) is None
    life = [dict(sheets[0], title="LIFE SAFETY PLAN - FIRST FLOOR"), sheets[1]]  # tags only some doors: its own reader
    assert P.route({"id": UID, "sheets": life, "items": []}, "How many doors are on this plan?", sheets[0]["id"]) is None
    # a schedule whose marks the counted plans hardly print (another wing's): the symbols answer, the schedule is said
    parts2 = [{"sheet": sheets[0]["id"], "answer": {"value": 15, "unit": "count", "confidence": 0.6, "evidence": [],
                                                    "solver": "openings"}}]
    monkeypatch.setattr(P, "_page_marks", lambda ab, pg: (pg == 1, {f"2{i:02d}A": [0, 0, 1, 1] for i in range(12)}
                                                          if pg == 1 else {"201A": [0, 0, 1, 1]}))
    b = P.combine(proj, "How many doors does the building have?", parts2)
    assert b["value"] == 15 and b["solver"] != "project/door_schedule" and "lists 12 doors" in b["assumptions"][-1]
    P._DS_CACHE.clear()


def test_a_one_storey_set_asks_its_floor_plan_that_prints_no_level():
    # FuT: only the trade plans print 'first floor' (FA-101 fire alarm, T-101 telecom, S-400 slab plan); the floor plan
    # A-111 prints no level and was left out as 'no level printed while the others name theirs', so a building door
    # count was asked on the slab plan. One storey named, by purpose plans only: the unlevelled floor plan is that storey
    def sh(n, title, storey, page):
        return {"id": f"upload/{UID}/fut.pdf#{page}", "file": f"upload/{UID}/fut.pdf", "page": page, "storey": storey,
                "level": "First floor" if storey else "", "building": "", "kind": "floor_plan", "role": "Floor plan",
                "title": title, "number": n, "partial": False, "user": {}, "label": f"{n} · {title}",
                "kind_source": "title block", "plan_line": "", "medium": "vector"}
    sheets = [sh("S-400", "OUTDOOR CARGO SUPPORT FOUNDATION AND SLAB PLAN", "1", 25), sh("A-111", "FLOOR PLAN", None, 29),
              sh("A-421", "OUTDOOR CARGO SUPPORT STRUCTURE PLAN", None, 37),
              sh("FA-101", "FIRE ALARM AND MASS NOTIFICATION PLAN", "1", 75), sh("T-101", "TELECOMMUNICATIONS PLAN FIRST FLOOR", "1", 91)]
    proj = {"id": UID, "sheets": sheets, "items": []}
    q = "How many doors are there in the whole building?"
    pl = P.plan(proj, q)
    assert [s["id"] for s in pl["sheets"]] == [sheets[1]["id"]] and pl["sheets"][0]["level"] == "First floor"
    assert "A-111" in pl["reason"] and "one-storey building" in pl["reason"]
    a = P.combine(proj, q, _parts(pl, [20]))
    assert a["value"] == 20 and not any("different floor" in x for x in a["assumptions"])
    assert P.route(proj, "How many doors are on the first floor?")["id"] == sheets[1]["id"]
    v = P.view(proj)  # the panel lists A-111 under First floor too, as the answers say
    first = next(lv for lv in v["levels"] if lv["storey"] == "1")
    assert sheets[1]["id"] in first["sheets"] and next(s for s in v["sheets"] if s["id"] == sheets[1]["id"])["level"] == "First floor"
    # a set whose general plans print their level keeps leaving an unlevelled plan out (an enlarged / typical plan)
    sheets2 = [dict(sheets[1], storey="1", level="First floor"), dict(sheets[2], title="TYPICAL UNIT PLAN", number="A-401")]
    pl2 = P.plan({"id": UID, "sheets": sheets2, "items": []}, q)
    assert [s["id"] for s in pl2["sheets"]] == [sheets2[0]["id"]]


def test_an_upper_floor_much_larger_than_the_floors_below_is_checked():
    # DfE secondary on an old footprint reader: L02 4,116.9 m² over L00 3,918.7 / L01 2,638.4 (its outline took the
    # roofs over the halls below): the total stays, as a best estimate, and the card / L02's row say to check L02
    def sheet(n, st):
        return {"id": f"{n}.pdf#0", "file": f"{n}.pdf", "page": 0, "number": "", "title": n, "kind": "floor_plan",
                "role": "Floor plan", "kind_source": "title block", "storey": st, "level": f"Level 0{st}",
                "building": "", "partial": False, "user": {}}
    proj = {"sheets": [sheet("a", "0"), sheet("b", "1"), sheet("c", "2")]}
    part = lambda v: {"value": v, "unit": "m2", "confidence": 0.5, "solver": "project/gross_area", "evidence": []}
    q = "What is the total floor area of the school?"
    a = P.combine(proj, q, [{"sheet": "a.pdf#0", "answer": part(3000.0)}, {"sheet": "b.pdf#0", "answer": part(2638.4)},
                            {"sheet": "c.pdf#0", "answer": part(4116.9)}])
    assert a["value"] == 9755.3 and a["confidence"] <= 0.35
    assert any(x.startswith("check: Level 02 measures 4,116.90 m², 37 % more than Level 00") for x in a["assumptions"])
    assert [r["note"] for r in a["per_sheet"]][2].startswith("37 % larger than Level 00")
    # an overhang of a few % (or a smaller upper floor) changes nothing; a door count is never checked this way
    b = P.combine(proj, q, [{"sheet": "a.pdf#0", "answer": part(3918.7)}, {"sheet": "b.pdf#0", "answer": part(2638.4)},
                            {"sheet": "c.pdf#0", "answer": part(4116.9)}])
    assert b["confidence"] == 0.5 and not any(x.startswith("check:") for x in b["assumptions"])
    d = P.combine(proj, "How many doors are there in the whole building?",
                  [{"sheet": s, "answer": {"value": v, "unit": "count", "confidence": 0.6, "evidence": []}}
                   for s, v in (("a.pdf#0", 10), ("b.pdf#0", 30), ("c.pdf#0", 30))])
    assert d["value"] == 70 and not any(x.startswith("check:") for x in d["assumptions"])
