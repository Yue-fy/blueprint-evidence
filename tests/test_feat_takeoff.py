"""F2 auto takeoff: room-name filter, slab names, exports (CSV / XLSX), path guard, feature flag, router."""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from stoa.features import takeoff as T

ROWS = [
    {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2", "value": 7.03,
     "confidence": 0.55, "method": "pipeline/plan_rooms", "status": "ok", "question": "What is the floor area of the Laundry?",
     "assumptions": ["scale: 1:200 printed"], "evidence": [{"file": "upload/ab/x.pdf", "page": 0,
                                                             "bbox": [841.2, 938.4, 890.1, 967.1], "bbox_space": "pdf_pt",
                                                             "note": "traced outline"}]},
    {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "baseboard", "unit": "m", "value": 10.13,
     "confidence": 0.5, "method": "pipeline/plan_rooms", "status": "ok", "evidence": []},
    {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "doors", "unit": "count", "value": 1,
     "confidence": 0.7, "method": "pipeline/openings", "status": "ok", "evidence": []},
    {"section": "room", "item": "Group Room", "copy": 2, "copies": 10, "quantity": "area", "unit": "m2", "value": 7.9,
     "confidence": 0.5, "method": "lineplan tracer (per label)", "status": "ok", "evidence": []},
    {"section": "sheet", "item": "Whole sheet", "quantity": "doors", "unit": "count", "value": 101, "confidence": 0.7,
     "method": "pipeline/openings", "status": "ok", "evidence": []},
    {"section": "room", "item": "Kitchen", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2", "value": None,
     "confidence": 0.0, "method": "", "status": "timeout", "evidence": []},
]


@pytest.mark.parametrize("text,ok", [
    ("Laundry", True), ("Multi-Purpose Hall", True), ("Secondary Classroom (Severe Needs)", True), ("Head's Office", True), ("English 1", True),
    ("past or present, is purely coincidental.", False), ("2026-01-12 Issued For Information", False),
    ("DESCRIPTION", False), ("SEN-DFE-XX-00-D-X-0201", False), ("Special School Plan Level 00", False), ("UP", False),
    ("12", False), ("Needs)", False), ("(Ground)", False), ("Secondary Classroom (Severe", True),
])
def test_room_name_filter(text, ok):
    assert T._room_name_ok(text) is ok


def test_merge_open_parens():
    labs = [{"text": "Secondary Classroom (Severe", "bbox": [100, 100, 160, 108], "pt": (130, 104)},
            {"text": "Needs)", "bbox": [118, 110, 142, 118], "pt": (130, 114)},
            {"text": "Laundry", "bbox": [400, 100, 430, 108], "pt": (415, 104)}]
    out = T._merge_open_parens(labs)
    assert [o["text"] for o in out] == ["Secondary Classroom (Severe Needs)", "Laundry"]


def test_slab_name():
    assert T._slab_name("300 SLAB-ON- GRADE W/ D16 @ 300 TOP FACE. BOTTOM FACE REINF") == "300 SLAB-ON-GRADE W/ D16 @ 300"
    assert T._slab_name("150 SLAB-ON-GRADE W/ D13 @ 300") == "150 SLAB-ON-GRADE W/ D13 @ 300"
    assert T._slab_name("300 SLAB-ON- GRADE W/ D22") == "300 SLAB-ON-GRADE W/ D22"


def test_csv_has_every_row_with_evidence():
    text = T.to_csv(ROWS, "upload/ab/x.pdf")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == len(ROWS)
    assert rows[0]["value"] == "7.03" and rows[0]["how_sure"] == "best estimate"
    assert rows[0]["evidence_box"].startswith("841 938 890 967") and rows[0]["file"] == "upload/ab/x.pdf"
    assert rows[3]["copy"] == "2/10"
    assert rows[5]["value"] == "" and rows[5]["how_sure"] == "no reading" and rows[5]["status"] == "timeout"


def test_wide_one_line_per_room_then_totals():
    head, body = T.wide(ROWS)
    assert head[:3] == ["Sheet", "Page", "Item"]
    laundry = body[0]
    assert laundry[2] == "Laundry" and laundry[5] == 7.03 and laundry[7] == 10.13 and laundry[9] == 1
    assert body[1][2:4] == ["Group Room", "2/10"]
    assert any(line[2].startswith("Whole sheet: Doors") and line[9] == 101 for line in body)


def test_xlsx_is_a_valid_workbook():
    data = T.to_xlsx(ROWS, "upload/ab/x.pdf")
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        assert {"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"} <= names
        s1 = z.read("xl/worksheets/sheet1.xml").decode()
        assert "Laundry" in s1 and "<v>7.03</v>" in s1
        import xml.dom.minidom as md

        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                md.parseString(z.read(n))  # well-formed XML
    else:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert wb.sheetnames == ["Takeoff", "Details"]


def test_resolve_refuses_paths_outside(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    (tmp_path / "ab12").mkdir()
    f = tmp_path / "ab12" / "plan.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert T.resolve("upload/ab12/plan.pdf") == (str(f.resolve()), "upload/ab12/plan.pdf")
    for bad in ("/etc/passwd", "upload/../../etc/passwd", "", "upload/ab12/../../x"):
        with pytest.raises(ValueError):
            T.resolve(bad)
    with pytest.raises(FileNotFoundError):
        T.resolve("upload/ab12/missing.pdf")


def test_job_ids_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path))
    with pytest.raises(ValueError):
        T.job_dir("../../etc")
    assert T.status("0123456789abcdef-p0-t1")["status"] == "unknown"


def test_router_is_off_without_the_flag(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()
    app.include_router(T.router)
    c = TestClient(app)
    monkeypatch.delenv("STOA_FEATURES", raising=False)
    assert c.get("/feat/takeoff/health").json()["enabled"] is False
    assert c.post("/feat/takeoff/start", json={"path": "upload/x/y.pdf"}).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "region,takeoff")
    r = c.post("/feat/takeoff/export", json={"rows": ROWS, "fmt": "csv", "file": "upload/ab/x.pdf"})
    assert r.status_code == 200 and "Laundry" in r.text and "x-takeoff.csv" in r.headers["content-disposition"]
    r = c.post("/feat/takeoff/export", json={"rows": ROWS, "fmt": "xlsx"})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.post("/feat/takeoff/start", json={"path": "/etc/passwd"}).status_code == 400


def test_features_registry_mounts_takeoff():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from stoa.features import include_feature_routers

    app = fastapi.FastAPI()
    assert "takeoff" in include_feature_routers(app, env="takeoff")
    assert TestClient(app).get("/feat/takeoff/health").status_code == 200


def test_stem_groups_singular_and_plural():
    assert T._stem("toilets") == T._stem("toilet") == "toilet"
    assert T._stem("group rooms") == "group room"
    assert T._stem("glass") == "glass" and T._stem("ups") == "ups"


def test_prior_guess_is_an_estimate():
    r = T.row_from_answer({"section": "room", "item": "PE Store", "quantity": "area", "unit": "m2"},
                          {"status": "ok", "value": 55.72, "unit": "m2", "confidence": 0.007, "solver": "pipeline/prior"})
    assert r["status"] == "estimate" and "not read from the drawing" in r["note"]
    r = T.row_from_answer({"section": "room", "item": "Laundry", "quantity": "area", "unit": "m2"},
                          {"status": "ok", "value": 7.03, "unit": "m2", "confidence": 0.55, "solver": "pipeline/plan_rooms"})
    assert r["status"] == "ok"


def test_printed_table_rows_keep_the_printed_unit():
    r = T.printed_row("upload/ab/a2.pdf", 0, {"label": "NORTH WING DORMITORY", "value": 6285.0, "unit": "ft2",
                                               "bbox": [10, 20, 200, 30]})
    assert r["section"] == "printed" and r["value"] == 6285.0 and r["unit"] == "ft2" and r["confidence"] == 0.9
    head, body = T.wide([r])
    assert body[0][2] == "NORTH WING DORMITORY: printed [ft²]" and body[0][5] == 6285.0


def _room_rows():
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    far = [[300, 0], [400, 0], [400, 100], [300, 100]]
    return [
        {"section": "room", "item": "Group Room", "copy": 1, "copies": 2, "quantity": "area", "value": 10.0,
         "confidence": 0.5, "evidence": [{"polygon": sq, "bbox": [0, 0, 100, 100], "bbox_space": "pdf_pt"}]},
        {"section": "room", "item": "Group Room", "copy": 2, "copies": 2, "quantity": "area", "value": 10.0,
         "confidence": 0.5, "evidence": [{"polygon": far, "bbox": [300, 0, 400, 100], "bbox_space": "pdf_pt"}]},
        {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "doors", "value": 1,
         "confidence": 0.7, "evidence": []},
    ]


def test_doors_by_room_uses_dr_rule_on_each_outline(monkeypatch):
    from stoa.evidence import linework as L
    from stoa.solvers import openings as O

    monkeypatch.setattr(L, "sheet_scales", lambda pdf, page=0: (100.0,))
    door = {"kind": "single", "center": (50, 0), "arcs": []}
    monkeypatch.setattr(O, "find_swings", lambda pdf, page, n: {"arcs": [1], "scale_n": 100.0})
    monkeypatch.setattr(O, "group_doors", lambda arcs: [door])
    monkeypatch.setattr(O, "doorways", lambda d: [((40, 0), (60, 0))])  # on the first room's top edge
    rows = _room_rows()
    assert T.doors_by_room("x.pdf", "upload/a/x.pdf", rows) == 2
    got = {(r["item"], r["copy"]): r["value"] for r in rows if r["quantity"] == "doors"}
    assert got == {("Group Room", 1): 1, ("Group Room", 2): 0, ("Laundry", 1): 1}


def test_windows_by_room_counts_boxes_on_the_outline(monkeypatch):
    import pymupdf

    from stoa.solvers import door_detect as D

    monkeypatch.setattr(T, "_det_view", lambda pdf: ("img.png", pymupdf.Matrix(1, 1), 1.0, 10.0, 1.0, 100.0))
    # boxes in render px (k = 1 -> pdf_pt): one window on room 1's left wall, one far away, one door
    monkeypatch.setattr(D, "_detect", lambda img, zoom, t: {"boxes": [[-5, 40, 5, 60, 0.9, "window"],
                                                                         [900, 900, 920, 920, 0.9, "window"],
                                                                         [40, -5, 60, 5, 0.9, "door"]]})
    rows = _room_rows()
    assert T.windows_by_room("x.pdf", "upload/a/x.pdf", rows) == 2
    got = {(r["item"], r["copy"]): r["value"] for r in rows if r["quantity"] == "windows"}
    assert got == {("Group Room", 1): 1, ("Group Room", 2): 0}


# ------------------------------------------------------------------ estimator view (captain 21:13 / 21:14, QA 21:19)
def test_flag_accepts_all(monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "all")
    assert T.enabled()
    monkeypatch.setenv("STOA_FEATURES", "region")
    assert not T.enabled()


def test_room_types_by_name():
    got = {n: T.room_type(n) for n in ("Secondary Classroom", "Changing Toilet", "Servery and Furniture",
                                       "Entrance Lobby", "Multi-Purpose Hall", "Server Room", "Secure Store",
                                       "Head's Office", "Female WC", "Saconcary Classro:m", "Xyzzy")}
    assert got == {"Secondary Classroom": "teaching", "Changing Toilet": "wet", "Servery and Furniture": "kitchen",
                   "Entrance Lobby": "circulation", "Multi-Purpose Hall": "hall", "Server Room": "plant",
                   "Secure Store": "storage", "Head's Office": "staff", "Female WC": "wet",
                   "Saconcary Classro:m": "teaching", "Xyzzy": "other"}
    # Maleva 18 (Estonian) and the Swiss floors (German): ESIK = entrance hall, not 'sick'
    assert [T.room_type(n) for n in ("TUALETTRUUM", "ELUTUBA+KÖÖK", "ESIK", "MAGAMISTUBA", "ABIRUUM", "SOOJUSSÕLM",
                                     "HOOLDUSTÖÖTAJAD", "Küche", "Treppenhaus", "Reduit")] == \
        ["wet", "living", "circulation", "sleep", "storage", "plant", "staff", "kitchen", "circulation", "storage"]


def _st():
    ev = lambda b, n="": {"file": "examples/x.pdf", "page": 0, "bbox": b, "bbox_space": "pdf_pt", "note": n}  # noqa
    rows = [
        {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
         "value": 7.0, "confidence": 0.55, "evidence": [{**ev([0, 0, 10, 10]), "polygon": [[0, 0], [10, 0], [10, 10]]}]},
        {"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "baseboard", "unit": "m",
         "value": 10.0, "confidence": 0.5, "assumptions": ["perimeter 11.0 m minus 1 opening(s)"],
         "evidence": [ev([0, 0, 10, 10])]},
        {"section": "room", "item": "Lobby", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
         "value": 7.75, "confidence": 0.5, "evidence": [ev([20, 0, 30, 10])]},
        {"section": "room", "item": "Lobby", "copy": 1, "copies": 1, "quantity": "baseboard", "unit": "m",
         "value": 1.69, "confidence": 0.45, "assumptions": ["perimeter 11.2 m minus 4 opening(s)"],
         "evidence": [ev([20, 0, 30, 10])]},
        {"section": "room", "item": "Office", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
         "value": 12.0, "confidence": 0.5, "evidence": [ev([40, 0, 50, 10])]},
        {"section": "room", "item": "Office", "copy": 1, "copies": 1, "quantity": "baseboard", "unit": "m",
         "value": 13.0, "confidence": 0.5, "assumptions": ["perimeter 14.0 m minus 1 opening(s)"],
         "evidence": [ev([40, 0, 50, 10])]},
        {"section": "room", "item": "PE Store", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
         "value": 55.7, "confidence": 0.007, "status": "estimate", "evidence": []},
        {"section": "room", "item": "Store", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
         "value": None, "confidence": 0.0, "status": "abstain", "evidence": []},
        {"section": "sheet", "item": "Whole sheet", "quantity": "doors", "unit": "count", "value": 3,
         "confidence": 0.7, "evidence": [ev([1, 1, 2, 2], "double door (2 leaves) 1"), ev([3, 3, 4, 4], "door 2"),
                                          ev([5, 5, 6, 6], "door 3")]},
        {"section": "total", "item": "Sum of room areas", "quantity": "area_sum", "unit": "m2", "value": 1.0},
    ]
    return {"status": "done", "file": "examples/x.pdf", "page": 0, "kind": "vector-pdf", "scale_n": 100.0,
            "sheet": "A-101", "rows": rows}


def test_finish_review_queue_stays_out_of_the_totals():
    st = T.finish(_st(), "upload/ab/x.pdf")
    rows = {(r["item"], r["quantity"]): r for r in st["rows"]}
    assert rows[("Lobby", "baseboard")]["review"].startswith("only 15% of the 11.2 m outline")
    assert "typical value" in rows[("PE Store", "area")]["review"]
    assert rows[("Store", "area")]["review"].startswith("no reading")
    assert rows[("Laundry", "area")]["review"] is None and rows[("Laundry", "area")]["wet"] is True
    assert rows[("Laundry", "baseboard")]["flags"] == [T.WET_NOTE]
    assert rows[("Whole sheet", "doors")]["leaves"] == 4
    assert all(e["file"] == "upload/ab/x.pdf" for r in st["rows"] for e in r.get("evidence") or [])
    assert all(r["sheet"] == "A-101" and r["page"] == 0 for r in st["rows"] if r["section"] != "total")
    tot = {r["quantity"]: r for r in st["rows"] if r["section"] == "total"}
    assert tot["area_sum"]["value"] == 26.75  # Laundry + Lobby + Office; PE Store (estimate) and Store left out
    assert "3 of 5 rooms" in tot["area_sum"]["assumptions"][0]
    assert tot["baseboard_sum"]["value"] == 23.0 and tot["baseboard_dry"]["value"] == 13.0  # Lobby in review
    assert tot["door_leaves"]["value"] == 4 and tot["rooms"]["value"] == 5
    assert st["review_count"] == 3


def test_summary_card_shape():
    s = T.summary(T.finish(_st()))
    assert s["status"] == "done" and s["sheet"] == "A-101" and s["scale"]["text"] == "1:100"
    assert s["area"]["value"] == 26.75 and s["area"]["focus"] == [0, 0, 50, 10]
    assert s["doors"]["sets"] == 3 and s["doors"]["leaves"] == 4 and s["doors"]["double"] == 1
    assert len(s["doors"]["marks"]) == 3
    types = {t["type"]: t for t in s["by_type"]}
    assert types["wet"]["area"] == 7.0 and types["wet"]["outlines"][0]["polygon"]
    assert types["storage"]["rooms"] == 2 and types["storage"]["area"] is None and types["storage"]["review"] == 2
    assert s["skirting"]["value"] == 23.0 and s["skirting"]["without_wet"] == 13.0
    assert {r["item"] for r in s["review"]} == {"Lobby", "PE Store", "Store"}
    head, body = T.wide(T.finish(_st())["rows"])
    assert head[0] == "Sheet" and body[0][0] == "A-101" and body[0][4] == "Toilets & wet rooms"
    assert "Review" in head[12] and body[1][12].startswith("Baseboard (skirting): only")
    assert "A-101" in T.to_csv(T.finish(_st())["rows"])


def test_summary_endpoint_and_examples(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    ex = tmp_path / "ex"
    ex.mkdir()
    (ex / "x.pdf").write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(T, "example_dirs", lambda: [ex])
    monkeypatch.setattr(T, "catalogue", lambda: [{"project": "p", "label": "L", "path": "examples/x.pdf"}])
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("STOA_FEATURES", "all")
    with pytest.raises(FileNotFoundError):
        T.resolve("examples/not-in-catalogue.pdf")
    app = fastapi.FastAPI()
    app.include_router(T.router)
    c = TestClient(app)
    r = c.get("/feat/takeoff/summary", params={"path": "examples/x.pdf"}).json()
    assert r["status"] == "none" and r["job"].endswith("-p0-" + T.ENGINE_V)
    d = T.cache_dir() / r["job"]
    d.mkdir(parents=True)
    T._write_json(d / "result.json", _st())
    s = c.get("/feat/takeoff/summary", params={"path": "examples/x.pdf"}).json()
    assert s["status"] == "done" and s["area"]["value"] == 26.75 and s["file"] == "examples/x.pdf"
    assert c.get("/feat/takeoff/summary", params={"path": "/etc/passwd"}).status_code == 400


def test_sheet_number_and_revision_from_the_title_block(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    f = tmp_path / "s.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    for i, (t, size) in enumerate((("Group Room", 4), ("REVISION", 5), ("C01", 12), ("SEN-DFE-XX-01-D-X-0202", 12),
                                   ("A2", 12), ("Lobby", 4), ("Store", 4))):
        pg.insert_text((20, 30 + 40 * i), t, fontsize=size)
    doc.save(f)
    assert T.sheet_rev(str(f), 0) == ("SEN-DFE-XX-01-D-X-0202", "C01")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    for i, (t, size) in enumerate((("S-100F", 28), ("SLAB", 5), ("NOTE", 5), ("C01", 12), ("x", 5))):
        pg.insert_text((20, 40 + 60 * i), t, fontsize=size)
    doc.save(tmp_path / "u.pdf")
    assert T.sheet_rev(str(tmp_path / "u.pdf"), 0) == ("S-100F", None)


def test_zero_doors_with_wall_openings_goes_to_review():
    st = _st()
    st["rows"].append({"section": "room", "item": "Office", "copy": 1, "copies": 1, "quantity": "doors", "unit": "count",
                       "value": 0, "confidence": 0.5, "evidence": []})
    rows = {(r["item"], r["quantity"]): r for r in T.finish(st)["rows"]}
    assert rows[("Office", "doors")]["review"].startswith("no door swing found on its outline, but its walls have 1")
