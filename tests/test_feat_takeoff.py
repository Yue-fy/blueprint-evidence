"""F2 auto takeoff: room-name filter, slab names, exports (CSV / XLSX), path guard, feature flag, router."""

from __future__ import annotations

import csv
import io
import zipfile

import json

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
    ("Enclosed Wall", True), ("Retaining Wall", False), ("Wall", False), ("Canopy", False), ("Courtyard", False), ("Enclosed Walkway", True),
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
    uid = "ab12ab12ab12"
    (tmp_path / uid).mkdir()
    f = tmp_path / uid / "plan.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert T.resolve(f"upload/{uid}/plan.pdf") == (str(f.resolve()), f"upload/{uid}/plan.pdf")
    # a picture the upload guessed a dataset style for: browser path upload/<id>/<style>/<name>, stored under data/
    g = tmp_path / uid / "data" / "cubicasa5k" / "plan.png"
    g.parent.mkdir(parents=True)
    g.write_bytes(b"\x89PNG")
    assert T.resolve(f"upload/{uid}/cubicasa5k/plan.png") == (str(g.resolve()), f"upload/{uid}/cubicasa5k/plan.png")
    for bad in ("/etc/passwd", "upload/../../etc/passwd", "", f"upload/{uid}/../../x", "upload/ab12/plan.pdf",
                f"upload/{uid}/a/b/plan.pdf"):
        with pytest.raises(ValueError):
            T.resolve(bad)
    with pytest.raises(FileNotFoundError):
        T.resolve(f"upload/{uid}/missing.pdf")


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


def test_a_window_belongs_to_the_one_room_on_the_inside_of_an_outside_wall():
    """F6 23:21 (Esplanades vs the IFC model): a box between two rooms counted for both, so inner toilets got the
    facade room's windows. Two rooms side by side (x 0-10 and 10.3-20, y 0-10; 10 pt per m), the outside below y 0."""
    from shapely.geometry import Polygon
    from shapely.geometry import box as sbox

    polys = [(("Bed", 1), Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])),
             (("WC", 1), Polygon([(103, 0), (200, 0), (200, 100), (103, 100)]))]
    boxes = [sbox(30, -3, 50, 0),     # in the outside wall of Bed (nothing below it)
             sbox(100, 40, 103, 60),  # in the wall between Bed and WC
             sbox(40, 40, 60, 50),    # inside Bed (a symbol, not in a wall)
             sbox(500, 500, 520, 503)]  # nowhere near a traced room
    got = T.window_owners(boxes, polys, 10.0)
    assert got == {0: ("Bed", 1), 1: None, 2: None}


def test_stored_window_rows_get_the_outside_wall_rule_at_read_time():
    rule = ["windows = V1's detected window symbols touching the room's traced outline (+0.25 m); a window between two "
            "rooms counts for both", "scale 1:100"]
    sq = lambda x0, x1: [[x0, 0], [x1, 0], [x1, 100], [x0, 100]]  # noqa: E731  (2.83 pt per m at 1:100)
    rows = [{"section": "room", "item": "Bed", "copy": 1, "quantity": "area", "value": 9.0,
             "evidence": [{"polygon": sq(0, 100)}]},
            {"section": "room", "item": "WC", "copy": 1, "quantity": "area", "value": 3.0,
             "evidence": [{"polygon": sq(101, 200)}]}]
    inner, outer = [100, 40, 101, 60], [30, -1, 50, 0]
    rows += [{"section": "room", "item": "Bed", "copy": 1, "quantity": "windows", "value": 2, "assumptions": rule,
              "method": "door_detect symbols on the room's outline (one pass per sheet)",
              "evidence": [{"bbox": inner}, {"bbox": outer}]},
             {"section": "room", "item": "WC", "copy": 1, "quantity": "windows", "value": 1, "assumptions": rule,
              "method": "door_detect symbols on the room's outline (one pass per sheet)", "evidence": [{"bbox": inner}]}]
    got = {r["item"]: r for r in T.finish({"kind": "vector-pdf", "sheet": "", "rows": rows})["rows"]
           if r["quantity"] == "windows"}
    assert got["Bed"]["value"] == 1 and got["Bed"]["evidence"] == [{"bbox": outer}]
    assert got["WC"]["value"] == 0 and "counts for none" in got["WC"]["assumptions"][0]


def test_ocr_names_on_a_scan_are_spelled_with_room_words():
    """F6 00:07: the scan PDF's rows read 'Croup Roo.m', 'Encloued' … Only garbled names change; foreign names stay."""
    got = {n: T.ocr_snap(n) for n in ("Croup Roo.m", "Saconcary Classro:m", "Accessitie Toilet", "Meoting Rocm",
                                      "Secondary Classroom (Severe Needs)", "TUALETTRUUM", "D&T Studio", "Staff Cmice")}
    assert got == {"Croup Roo.m": "Group Room", "Saconcary Classro:m": "Secondary Classroom",
                   "Accessitie Toilet": "Accessible Toilet", "Meoting Rocm": "Meeting Room",
                   "Secondary Classroom (Severe Needs)": "Secondary Classroom (Severe Needs)",
                   "TUALETTRUUM": "TUALETTRUUM", "D&T Studio": "D&T Studio", "Staff Cmice": "Staff Cmice"}
    st = {"kind": "scanned pdf (traced)", "sheet": "", "rows": [
        {"section": "room", "item": "Croup Roo.m", "copy": 1, "quantity": "area", "value": 20.0, "confidence": 0.5}]}
    r = T.finish(st)["rows"][0]
    assert r["item"] == "Group Room" and r["ocr_name"] == "Croup Roo.m" and r["room_type"] == "teaching"
    st["kind"] = "vector-pdf"  # a PDF's own text is as printed
    assert T.finish(st)["rows"][0]["item"] == "Croup Roo.m"
    # a snapped name joining a printed one: numbered again, never one line for two rooms
    two = {"kind": "image (traced)", "sheet": "", "rows": [
        {"section": "room", "item": "Seoundary Classmom", "copy": 1, "copies": 1, "quantity": "area", "value": 50.0},
        {"section": "room", "item": "Secondary Classroom", "copy": 1, "copies": 1, "quantity": "area", "value": 52.0},
        {"section": "room", "item": "Seoundary Classmom", "copy": 1, "copies": 1, "quantity": "doors", "value": 1}]}
    got = [(r["item"], r["copy"], r["copies"], r["value"]) for r in T.finish(two)["rows"] if r["section"] == "room"]
    assert got == [("Secondary Classroom", 2, 2, 50.0), ("Secondary Classroom", 1, 2, 52.0),
                   ("Secondary Classroom", 2, 2, 1)]
    assert [T.room_type(n) for n in ("Buero 2", "Kueche", "Waschkueche", "Queue Room")] == \
        ["staff", "kitchen", "wet", "other"]  # F8 23:57: German names typed without umlauts


def test_a_room_tag_needs_a_name(monkeypatch):
    """A 600-dpi trace of AFCEC A-2's area table / key plan gave 'room tags' named '095 SF', '6,092', '911 SF ,240 SF'
    (4 junk rooms, a junk net total). A tag's name needs a word that is not an area unit; 'WC' is a name."""
    import stoa.sheet.labels as SL

    tags = [{"name": n, "value": 9.0, "unit": "ft2", "bbox": [0, 0, 1, 1]} for n in
            ("095 SF", "6,092", "911 SF ,240 SF", "WC", "NCOIC Office")]
    monkeypatch.setattr(SL, "as_area_tags", lambda pdf, q, page: tags)
    assert [t["name"] for t in T.area_tags("x.pdf")] == ["WC", "NCOIC Office"]


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
                   "Entrance Lobby": "circulation", "Multi-Purpose Hall": "hall", "Server Room": "storage",
                   "Secure Store": "storage", "Head's Office": "staff", "Female WC": "wet",
                   "Saconcary Classro:m": "teaching", "Xyzzy": "other"}
    # Maleva 18 (Estonian) and the Swiss floors (German): ESIK = entrance hall, not 'sick'
    assert [T.room_type(n) for n in ("TUALETTRUUM", "ELUTUBA+KÖÖK", "ESIK", "MAGAMISTUBA", "ABIRUUM", "SOOJUSSÕLM",
                                     "HOOLDUSTÖÖTAJAD", "Küche", "Treppenhaus", "Reduit")] == \
        ["wet", "living", "circulation", "living", "storage", "storage", "staff", "kitchen", "circulation", "storage"]
    # the head noun (last type word) names the room; a wet word anywhere keeps it wet (SEC L00, unseen)
    assert [T.room_type(n) for n in ("Sports Equipment Store", "Reception Classroom", "Dining Hall", "Toilet Lobby",
                                     "Art Room (multi-purpose)", "Wohnen/Essen")] == \
        ["storage", "teaching", "hall", "wet", "teaching", "living"]


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
    assert head[0] == "Sheet" and body[0][0] == "A-101" and body[0][4] == "Wet rooms"
    assert "Review" in head[12] and body[1][12].startswith("Skirting (baseboard): only")
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
    vec = {(r["item"], r["quantity"]): r for r in T.finish(json.loads(json.dumps(st)))["rows"]}
    # a vector sheet reads every drawn door: an opening with none is an open doorway (F6 23:23, Esplanades)
    assert not vec[("Office", "doors")].get("review")
    assert "1 opening in its walls with no door drawn (open doorway)" in vec[("Office", "doors")]["assumptions"]
    st["kind"] = "image (traced)"  # arcs can be lost in a tracing: ask a person
    rows = {(r["item"], r["quantity"]): r for r in T.finish(st)["rows"]}
    assert rows[("Office", "doors")]["review"].startswith("no door swing found on its outline, but its walls have 1")


def test_direct_reader_counts_on_the_takeoffs_page(monkeypatch, tmp_path):
    from stoa.solvers import openings as O

    seen = {}

    class Fake:
        def solve(self, path, question):
            seen["pages"] = O.pick_pages(path, question)

            class A:
                def model_dump(self):
                    return {"value": 1, "evidence": []}

            return A()

    import stoa.solvers as S

    monkeypatch.setattr(S, "get_solver", lambda name, **k: Fake())
    f = tmp_path / "set.pdf"
    f.write_bytes(b"%PDF")
    T._direct("openings", str(f), "upload/a/set.pdf", "How many doors does the Kitchen have?", 2)
    assert seen["pages"][0] == [2]
    assert O.pick_pages is not None and O.pick_pages.__name__ == "pick_pages"


def test_ifc_spaces_take_the_models_room(monkeypatch):
    from stoa.evidence import ifcplan_pdf as I

    sq = lambda x0, y0, x1, y1: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]  # noqa: E731
    spaces = [{"label": "ESIK 6-2", "polygon": sq(0, 0, 10, 10), "bbox": [0, 0, 10, 10], "m2": 3.01, "scale": 100,
               "scale_printed": True},
              {"label": "ESIK 5-2", "polygon": sq(20, 0, 30, 10), "bbox": [20, 0, 30, 10], "m2": 4.92, "scale": 100,
               "scale_printed": True},
              {"label": "KORIDOR 119", "polygon": sq(40, 0, 60, 10), "bbox": [40, 0, 60, 10], "m2": 63.83,
               "scale": 100, "scale_printed": True}]
    monkeypatch.setattr(I, "space_polygons", lambda pdf: spaces)
    rows = [{"section": "room", "item": "ESIK", "copy": 1, "copies": 2, "quantity": "area", "value": 3.0,
             "confidence": 0.5, "label_box": [4, 4, 6, 6], "evidence": [{"bbox": [0, 0, 10, 10]}]},
            {"section": "room", "item": "ESIK", "copy": 1, "copies": 2, "quantity": "doors", "value": 1,
             "confidence": 0.5, "label_box": [4, 4, 6, 6]},
            {"section": "room", "item": "ESIK", "copy": 2, "copies": 2, "quantity": "area", "value": 5.3,
             "confidence": 0.5, "label_box": [24, 4, 26, 6], "evidence": []}]
    got = T.ifc_spaces("x.pdf", "upload/a/x.pdf", rows)
    assert got == {"spaces": 3, "linked": 2, "agree": 1, "differ": 1, "added": 1}
    a = {r["item"]: r for r in rows if r["quantity"] == "area"}
    assert a["ESIK 6-2"]["value"] == 3.01 and a["ESIK 6-2"]["check"]["agree"] is True
    assert a["ESIK 5-2"]["value"] == 4.92 and a["ESIK 5-2"]["check"]["diff_pct"] == 7.7
    assert a["KORIDOR 119"]["value"] == 63.83 and "no room label" in a["KORIDOR 119"]["assumptions"][0]
    assert [r["item"] for r in rows if r["quantity"] == "doors"] == ["ESIK 6-2"]


def test_a_users_measurement_moves_a_review_row_into_the_totals(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path))
    key = "0123456789abcdef-p0-" + T.ENGINE_V
    d = T.cache_dir() / key
    d.mkdir()
    T._write_json(d / "result.json", _st())
    st = T.status(key)
    pe = next(r for r in st["rows"] if r["item"] == "PE Store" and r["quantity"] == "area")
    assert pe["review"] and next(r for r in st["rows"] if r["quantity"] == "area_sum")["value"] == 26.75
    with pytest.raises(ValueError):
        T.fix(key, "../x", 3.0)
    T.fix(key, pe["id"], 51.0, "m2", [[0, 0], [10, 0], [10, 10]], "pdf_pt", "PE Store")
    st = T.status(key)
    pe = next(r for r in st["rows"] if r["id"] == pe["id"])
    assert pe["value"] == 51.0 and pe["review"] is None and pe["confidence"] == 0.95
    assert pe["reading"]["value"] == 55.7 and pe["evidence"][0]["polygon"]
    assert next(r for r in st["rows"] if r["quantity"] == "area_sum")["value"] == 77.75
    T.fix(key, pe["id"], None)
    assert next(r for r in T.status(key)["rows"] if r["quantity"] == "area_sum")["value"] == 26.75


def test_a_persons_fix_is_theirs_alone(tmp_path, monkeypatch):
    """F6 23:36: jobs are content-keyed and shared by every visitor (and every server on the cache): one judge's Measure
    must not show as 'measured by a person' to the next one; the old shared fixes.json is not applied any more."""
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path))
    key = "0123456789abcdef-p0-" + T.ENGINE_V
    d = T.cache_dir() / key
    d.mkdir()
    T._write_json(d / "result.json", _st())
    pe = next(r for r in T.status(key)["rows"] if r["item"] == "PE Store" and r["quantity"] == "area")
    T._write_json(d / "fixes.json", {pe["id"]: {"value": 40.0, "unit": "m2", "item": "PE Store", "at": "21:00"}})
    total = lambda st: next(r for r in st["rows"] if r["quantity"] == "area_sum")["value"]  # noqa: E731
    assert total(T.status(key)) == 26.75  # a rehearsal's fix in the old shared file is ignored
    alice, bob = "a1b2c3d4e5f6a7b8c9d0e1f2", "ffffeeeeddddccccbbbbaaaa"
    T.fix(key, pe["id"], 51.0, "m2", [[0, 0], [10, 0], [10, 10]], "pdf_pt", "PE Store", visitor=alice)
    assert total(T.status(key, visitor=alice)) == 77.75
    assert total(T.status(key, visitor=bob)) == 26.75 and total(T.status(key)) == 26.75
    assert not list(d.glob("*" + alice + "*"))  # the id itself is not written to disk
    with pytest.raises(ValueError):
        T.status(key, visitor="../../etc")
    T.fix(key, pe["id"], None, visitor=alice)
    assert total(T.status(key, visitor=alice)) == 26.75


def test_start_over_forgets_this_visitors_fixes(tmp_path, monkeypatch):
    """captain 01:18 'Start over': this visitor's Measure fixes go on every sample sheet; their own upload keeps them
    when it is in keep (they said 'keep my corrections'); another visitor's fixes stay."""
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path))
    alice, bob = "a1b2c3d4e5f6a7b8c9d0e1f2", "ffffeeeeddddccccbbbbaaaa"
    total = lambda st: next(r for r in st["rows"] if r["quantity"] == "area_sum")["value"]  # noqa: E731
    keys = []
    for i, file in enumerate(("examples/dfe-l00.pdf", "upload/0123456789ab/mine.pdf")):
        key = f"{i}123456789abcdef-p0-" + T.ENGINE_V
        d = T.cache_dir() / key
        d.mkdir()
        T._write_json(d / "result.json", {**_st(), "file": file})
        pe = next(r for r in T.status(key)["rows"] if r["item"] == "PE Store" and r["quantity"] == "area")
        for v in (alice, bob):
            T.fix(key, pe["id"], 51.0, "m2", None, None, "PE Store", visitor=v)
        keys.append(key)
    assert T.forget(alice, keep=["upload/0123456789ab/mine.pdf"]) == 1
    assert total(T.status(keys[0], visitor=alice)) == 26.75  # the sample sheet reads as it first did
    assert total(T.status(keys[1], visitor=alice)) == 77.75  # their own upload kept its measurement
    assert total(T.status(keys[0], visitor=bob)) == 77.75  # nobody else's fixes are touched
    assert T.forget(alice) == 1 and total(T.status(keys[1], visitor=alice)) == 26.75
    assert T.forget("") == 0
    with pytest.raises(ValueError):
        T.forget("../../etc")


def test_a_count_is_no_skirting_or_area():
    """UX 02:05 (the demo's Swiss floor): the pipeline answered 'How many linear metres of baseboard does Zimmer need'
    with a door count (unit 'count', door marks as evidence) -> 'Skirting · 3 rooms · 22 m' on a 25-room floor."""
    st = _st()
    st["rows"] = [r for r in st["rows"] if not (r["item"] == "Office" and r["quantity"] == "baseboard")]
    st["rows"].append({"section": "room", "item": "Office", "copy": 1, "copies": 1, "quantity": "baseboard",
                       "unit": "count", "value": 8, "confidence": 0.8, "method": "pipeline/dxf",
                       "evidence": [{"bbox": [0, 0, 1, 1], "note": "door 15"}]})
    st2 = T.finish(st)
    off = next(r for r in st2["rows"] if r["item"] == "Office" and r["quantity"] == "baseboard")
    assert off["value"] is None and off["evidence"] == [] and off["unit"] == "m" and "count (8)" in off["note"]
    sk = T.summary(st2)["skirting"]
    assert sk is None or sk["rooms"] == sum(1 for r in st2["rows"] if r["quantity"] == "baseboard"
                                            and r["section"] == "room" and isinstance(r["value"], (int, float)))
    if sk:
        assert sk["labelled"] >= sk["rooms"]


def test_two_labels_on_one_outline_count_once():
    """The demo's DfE L00 PNG: OCR read 'Enclosed Walkway' as 'Enclosed Wall' + 'Walkway', both traced the same
    119.39 m² outline -> the net floor area counted it twice (1,736.96 vs the PDF twin's 1,613.2)."""
    st = _st()
    poly = [[0, 0], [100, 0], [100, 10], [0, 10]]
    for name, conf in (("Enclosed Wall", 0.5), ("Walkway", 0.5)):
        st["rows"].append({"section": "room", "item": name, "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
                           "value": 119.39, "confidence": conf, "status": "ok",
                           "evidence": [{"page": 0, "bbox": [315, 3128, 1975, 3313], "polygon": poly}]})
        st["rows"].append({"section": "room", "item": name, "copy": 1, "copies": 1, "quantity": "baseboard",
                           "unit": "m", "value": 63.1, "confidence": 0.45, "status": "ok",
                           "evidence": [{"page": 0, "bbox": [315, 3128, 1975, 3313]}]})
    before = T.summary(T.finish(_st()))["area"]["value"]
    st2 = T.finish(st)
    rows = {(r["item"], r["quantity"]): r for r in st2["rows"]}
    assert not rows[("Walkway", "area")]["review"] and not rows[("Walkway", "baseboard")]["review"]
    assert rows[("Enclosed Wall", "area")]["review"].startswith("same outline as Walkway (119.39 m²): counted once")
    assert rows[("Enclosed Wall", "baseboard")]["review"] and rows[("Enclosed Wall", "area")]["same_outline"]
    assert T.summary(st2)["area"]["value"] == round(before + 119.39, 2)
    # a room of the same size elsewhere on the sheet is another room
    st3 = T.finish({**st, "rows": [r for r in st["rows"] if r["item"] != "Enclosed Wall"] + [
        {**r, "evidence": [{"page": 0, "bbox": [315, 4128, 1975, 4313], "polygon": poly}]}
        for r in st["rows"] if r["item"] == "Enclosed Wall"]})
    assert not any(r.get("same_outline") for r in st3["rows"])
    T.SAME_OUTLINE = False
    try:
        assert not any(r.get("same_outline") for r in T.finish(st)["rows"])
    finally:
        T.SAME_OUTLINE = True


def test_skirting_that_cannot_fit_the_room_goes_to_review():
    st = _st()
    st["rows"].append({"section": "room", "item": "Office", "copy": 1, "copies": 1, "quantity": "baseboard",
                       "unit": "m", "value": 1.0, "confidence": 0.75, "evidence": []})
    st["rows"] = [r for r in st["rows"] if not (r["item"] == "Office" and r["quantity"] == "baseboard"
                                                 and r["value"] == 13.0)]
    rows = {(r["item"], r["quantity"]): r for r in T.finish(st)["rows"]}
    assert rows[("Office", "baseboard")]["review"].startswith("1 m of skirting does not fit a 12 m² room")


def test_components_for_the_convention_switch():
    st = _st()
    st["rows"][1]["parts"] = {"perimeter_m": 11.0, "openings_m": [1.0]}
    st["rows"].append({"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": "doors",
                       "unit": "count", "value": 1, "confidence": 0.7,
                       "evidence": [{"bbox": [0, 0, 1, 1], "note": "double door (2 leaves) 1"}]})
    rows = T.finish(st)["rows"]
    c = next(r for r in rows if r["item"] == "Laundry" and r["quantity"] == "area")["components"]
    assert c["room_type"] == "wet" and c["wet"] and c["area_m2"] == 7.0 and c["perimeter_m"] == 11.0
    assert c["skirting_net_m"] == 10.0 and c["openings_m"] == [1.0] and c["wall_thickness_m"] is None
    assert c["doors"] == 1 and c["door_leaves"] == 2 and c["leaves_per_door"] == [2] and c["confidence"] == 0.5
    lobby = next(r for r in rows if r["item"] == "Lobby" and r["quantity"] == "area")["components"]
    assert lobby["perimeter_m"] == 11.2 and lobby["openings_m"] is None


def test_picture_paper_falls_back_like_the_upload_solver(monkeypatch):
    """A phone photo / odd-size scan (no DPI tag, no standard export size) still gets a paper size: the photographed
    sheet's corners, then the scale bar x the printed 1:N (the upload solver's order). The first source that answers wins."""
    from types import SimpleNamespace

    from stoa.formats import paper as P
    from stoa.formats import vectorize as V
    from stoa.solvers.raster import photo as PH
    from stoa.solvers.raster import textlayer as TL

    layer = SimpleNamespace(items=[SimpleNamespace(text="SCALE 1:200")])
    monkeypatch.setattr(V, "paper", lambda p: None)
    monkeypatch.setattr(TL, "text_layer", lambda p: layer)
    monkeypatch.setattr(P, "paper_of_image", lambda p, t: None)
    monkeypatch.setattr(PH, "named_paper", lambda p, t: None)
    monkeypatch.setattr(P, "paper_of_bar", lambda p, lay: (160.0, "custom", "from its scale bar"))
    assert T.paper_source("x.jpg") == ((160.0, "custom"), "from its scale bar")
    monkeypatch.setattr(PH, "named_paper", lambda p, t: (210.0, "A1"))
    hit, how = T.paper_source("x.jpg")
    assert hit == (210.0, "A1") and "corners" in how
    monkeypatch.setattr(P, "paper_of_bar", lambda p, lay: None)
    monkeypatch.setattr(PH, "named_paper", lambda p, t: None)
    assert T.paper_source("x.jpg") == (None, "")
    assert "Measure" in T.UNTRACED


def test_stored_jobs_drop_labels_that_are_no_room_at_read_time():
    """A job cached before the wall / canopy filter (a canopy label traced as a room) is cleaned when read; the other
    rows keep their ids, so a person's measurement stays on its row."""
    st = {"file": "upload/ab/x.pdf", "page": 0, "sheet": "A-1", "revision": None, "rows": [
        {"section": "room", "item": "Canopy", "quantity": "area", "unit": "m2", "value": 119.0, "confidence": 0.5,
         "status": "ok", "evidence": []},
        {"section": "room", "item": "Laundry", "quantity": "area", "unit": "m2", "value": 7.0, "confidence": 0.3,
         "status": "ok", "evidence": []}]}
    out = T.finish(st, "upload/ab/x.pdf", fixes={"r1": {"value": 7.1, "unit": "m2", "item": "Laundry", "at": "22:30"}})
    rooms = [r for r in out["rows"] if r.get("section") == "room"]
    assert [r["item"] for r in rooms] == ["Laundry"] and rooms[0]["id"] == "r1" and rooms[0]["value"] == 7.1


def test_model_check_lists_rooms_where_drawing_and_model_disagree():
    rows = [{"id": "r0", "section": "room", "quantity": "area", "item": "VALVERUUM", "value": 10.32,
             "check": {"value": None, "diff_pct": None, "agree": False}},
            {"id": "r1", "section": "room", "quantity": "area", "item": "ABIRUUM", "value": 22.4,
             "check": {"value": 24.27, "diff_pct": 8.3, "agree": False}},
            {"id": "r2", "section": "room", "quantity": "area", "item": "TUALETTRUUM", "value": 5.83,
             "check": {"value": 5.99, "diff_pct": 2.7, "agree": False}},
            {"id": "r3", "section": "room", "quantity": "area", "item": "KORIDOR", "value": 40.0}]
    mc = T._model_check({"spaces": 4, "linked": 3, "agree": 0}, rows)
    assert [o["item"] for o in mc["differ"]] == ["VALVERUUM", "ABIRUUM"] and mc["linked"] == 3
    assert T._model_check(None, rows) is None


def test_printed_area_table_grouped_by_type_and_checked_against_its_total():
    """AFCEC A-2: wing rows -> dormitory / circulation (incl. the common space's) / common space; the types add up to the
    printed TOTAL SECOND FLOOR (a table read whole)."""
    vals = [("WEST WING DORMITORY", 6095), ("WEST WING CIRCULATION", 914), ("TOTAL WEST WING", 7009),
            ("EAST WING DORMITORY", 6092), ("EAST WING CIRCULATION", 913), ("NORTH WING DORMITORY", 6285),
            ("NORTH WING CIRCULATION", 1403), ("COMMON SPACE", 2911), ("COMMON SPACE CIRCULATION", 1240),
            ("UTILITY", 328), ("SUBTOTAL", 26181), ("EXTERIOR STAIR (1/2 SF)", 381), ("TOTAL SECOND FLOOR", 26562)]
    rows = [{"id": f"r{i}", "section": "printed", "quantity": "area", "item": n, "value": float(v), "unit": "ft2",
             "confidence": 0.9} for i, (n, v) in enumerate(vals)]
    out = T.printed_by_type(rows)
    by = {t["type"]: t["value"] for t in out["types"]}
    assert by["dormitory"] == 18472 and by["circulation"] == 4470 and by["common space"] == 2911
    assert out["check"]["printed"] == "TOTAL SECOND FLOOR" and out["check"]["agree"]
    assert T.printed_by_type(rows[:1]) is None


def test_paper_tie_broken_by_the_drawings_dimension_strings(tmp_path, monkeypatch):
    """10200 x 6600 px fits ANSI D @ 300 and Tabloid @ 600; the sheet's 1" = 40' makes its dimension lines 7.5 or 15
    px per ft: the paper that >= 2 drawn dimensions agree with wins (AFCEC A-2: Tabloid)."""
    from types import SimpleNamespace

    from PIL import Image

    from stoa.formats import paper as P
    from stoa.formats import vectorize as V
    from stoa.solvers.raster import dimline as DL
    from stoa.solvers.raster import textlayer as TL

    img = tmp_path / "sheet.png"
    Image.new("L", (1020, 660), 255).save(img)  # a tenth of the size: 30 / 60 dpi
    monkeypatch.setattr(P, "candidates", lambda size: [("ANSI D", 300), ("Tabloid", 600)])
    monkeypatch.setattr(V, "_scale_n", lambda items: 480.0)
    dims = [{"feet": f, "box": [0, i * 10, 10, i * 10 + 5], "text": f"{f}'"} for i, f in enumerate((286, 323, 52, 40))]
    monkeypatch.setattr(TL, "dimensions", lambda layer: dims)
    px = {286: 286 * 1.5, 323: 323 * 1.51, 52: 52 * 0.7, 40: 40 * 3}  # 2 match Tabloid (1.5 px/ft), none ANSI D (0.75)
    monkeypatch.setattr(DL, "dim_line", lambda g, box: {"px": px[next(d["feet"] for d in dims if d["box"] == box)]})
    dpi, sheet, how = T.paper_by_dims(str(img), SimpleNamespace(items=[]))
    assert sheet == "Tabloid" and abs(dpi - 60) < 0.5 and "2 of 4" in how
    px.update({286: 286 * 0.75, 323: 323 * 0.75})  # now 2 for ANSI D, 0 for Tabloid ... and a tie is no answer
    assert T.paper_by_dims(str(img), SimpleNamespace(items=[]))[1] == "ANSI D"
    px.update({52: 52 * 1.5, 40: 40 * 1.5})
    assert T.paper_by_dims(str(img), SimpleNamespace(items=[])) is None


def test_a_sheet_door_count_no_building_has_goes_to_review():
    """AFCEC A-2 traced from its picture: 494 'doors' on a 26,562 ft² (2,468 m²) floor -> Review, and the door-leaves
    total leaves it out; 91 doors on DfE's 1,717 m² stay."""
    def st(n, rows):
        return {"file": "x.png", "page": 0, "sheet": "A-2", "revision": None, "rows": [
            *rows, {"section": "sheet", "item": "Whole sheet", "quantity": "doors", "unit": "count", "value": n,
                    "confidence": 0.5, "status": "ok", "method": "pipeline/openings", "evidence": []}]}
    printed = [{"section": "printed", "item": "TOTAL SECOND FLOOR", "quantity": "area", "unit": "ft2", "value": 26562.0,
                "confidence": 0.9, "status": "ok", "evidence": []}]
    out = T.finish(st(494, printed), "x.png")
    d = next(r for r in out["rows"] if r.get("section") == "sheet")
    assert d["review"] and "one per 5.0 m²" in d["review"]
    assert not any(r.get("quantity") == "door_leaves" for r in out["rows"])
    rooms = [{"section": "room", "item": f"Room {i}", "quantity": "area", "unit": "m2", "value": 9.0, "confidence": 0.5,
              "status": "ok", "evidence": []} for i in range(40)]  # a scan with half its labels read: 360 m² summed
    assert not next(r for r in T.finish(st(95, rooms), "x.png")["rows"] if r.get("section") == "sheet").get("review")
    assert "for 40 labelled rooms" in next(r for r in T.finish(st(200, rooms), "x.png")["rows"]
                                           if r.get("section") == "sheet")["review"]


def test_counts_on_a_traced_picture_without_rooms_wait_in_review():
    st = {"file": "x.png", "page": 0, "sheet": "A-2", "revision": None, "traced": "Tabloid at 353 dpi", "rows": [
        {"section": "sheet", "item": "Whole sheet", "quantity": "windows", "unit": "count", "value": 33,
         "confidence": 0.5, "status": "ok", "method": "pipeline/openings", "evidence": []}]}
    assert "no room outlines" in T.finish(st, "x.png")["rows"][0]["review"]
    st.pop("traced")
    assert not T.finish(st, "x.png")["rows"][0].get("review")


def test_rooms_with_one_name_are_summed_incl_qualified_variants():
    """DfE L00: 6 'Secondary Classroom' + 2 'Secondary Classroom (Severe …)' = the 8 X1 counts (gold total 417.49)."""
    rows = [{"id": f"r{i}", "section": "room", "item": n, "copy": c, "copies": k, "quantity": "area", "unit": "m2",
             "value": 52.0, "confidence": 0.5} for i, (n, c, k) in enumerate(
        [*[("Secondary Classroom", c, 6) for c in range(1, 7)], ("Secondary Classroom (Severe", 1, 1),
         ("Secondary Classroom (Severe)", 1, 1), ("Laundry", 1, 1)])]
    rows[0]["review"] = "rough"
    out = T.by_name(rows)
    assert len(out) == 1 and out[0]["item"] == "Secondary Classroom" and out[0]["copies"] == 8
    assert out[0]["area"]["used"] == 7 and out[0]["area"]["value"] == 364.0


def test_exports_carry_the_summed_lines_outside_the_rooms():
    st = {"file": "x.pdf", "page": 0, "sheet": "A-1", "revision": None, "rows": [
        *[{"section": "room", "item": "Secondary Classroom", "copy": c, "copies": 2, "quantity": "area", "unit": "m2",
           "value": 52.0, "confidence": 0.5, "status": "ok", "evidence": [{"bbox": [0, 0, 1, 1]}]} for c in (1, 2)],
        *[{"section": "printed", "item": n, "quantity": "area", "unit": "ft2", "value": v, "confidence": 0.9,
           "status": "ok", "evidence": []} for n, v in (("WEST WING DORMITORY", 10.0), ("EAST WING DORMITORY", 5.0),
                                                        ("TOTAL", 15.0))]]}
    st = T.finish(st, "x.pdf")
    extra = T.sum_rows(st)
    assert [(r["section"], r["item"], r["value"]) for r in extra] == [("same_name", "Secondary Classroom x2", 104.0),
                                                                     ("printed_type", "Dormitory", 15.0)]
    head, body = T.wide([*st["rows"], *extra])
    items = [line[2] for line in body]
    assert "Secondary Classroom x2: sum of the rooms with this name" in items
    assert any(i.startswith("Dormitory: printed, summed by type") for i in items)
    assert sum(1 for line in body if line[2] == "Secondary Classroom") == 2  # the rooms themselves: one line per copy
    assert "same_name" in T.to_csv(extra)


def test_photo_evidence_goes_back_onto_the_photo_through_the_homography():
    rows = [{"section": "room", "item": "Laundry", "quantity": "area", "value": 7.0, "confidence": 0.6,
             "label_box": [1, 1, 2, 2], "evidence": [{"bbox": [10, 10, 20, 20], "polygon": [[10, 10], [20, 10], [20, 20]]}]},
            {"section": "printed", "item": "TOTAL", "quantity": "area", "value": 9.0, "_px": True,
             "evidence": [{"bbox": [5, 5, 6, 6]}]}]
    H = [[1, 0, 100], [0, 1, 50], [0, 0, 1]]  # deskewed px -> photo px: a shift
    T.to_image(rows, "upload/a/p.jpg", 2.0, "A2 at 155 dpi", H)
    e = rows[0]["evidence"][0]
    assert e["bbox"] == [120, 70, 140, 90] and e["polygon"][0] == [120, 70] and e["bbox_space"] == "image_px"
    assert rows[0]["label_box"] == [102, 52, 104, 54] and rows[0]["confidence"] == T.TRACED_CAP
    assert rows[1]["evidence"][0]["bbox"] == [5, 5, 6, 6]  # read on the photo itself: untouched


@pytest.mark.parametrize("names,merged", [
    (["Secondary Classroom", "Secondary Classroom", "Socondary Classrcom"], True),  # OCR on a photo
    (["Plant Room", "Plant Room", "Parent Room"], False),  # close letters, another room type
    (["Female WC", "Female WC", "Male WC"], False),  # an opposite qualifier
    (["KORIDOR 402", "KORIDOR 402", "KORIDOR 405"], False),  # room numbers
    (["Whg. B Zimmer", "Whg. B Zimmer", "Whg. C Zimmer"], False),  # unit letters
])
def test_misspelt_room_names_join_their_group_but_different_rooms_do_not(names, merged):
    rows = [{"id": f"r{i}", "section": "room", "item": n, "copy": i + 1, "copies": 3, "quantity": "area", "unit": "m2",
             "value": 10.0, "confidence": 0.5} for i, n in enumerate(names)]
    out = T.by_name(rows)
    assert (out[0]["copies"] == 3) is merged and out[0]["item"] == names[0]


def test_same_name_group_is_labelled_with_the_well_spelt_name():
    # a photo's OCR: the garbled copy comes first and is as frequent as the right spelling
    rows = [{"id": f"r{i}", "section": "room", "item": n, "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
             "value": 4.0, "confidence": 0.5} for i, n in enumerate(["Accessitie Toilet", "Accessible Toilet"])]
    out = T.by_name(rows)
    assert out[0]["copies"] == 2 and out[0]["item"] == "Accessible Toilet"
    assert out[0]["names"] == ["Accessible Toilet", "Accessitie Toilet"]


def test_measured_area_gives_the_rooms_skirting_from_that_outline(tmp_path, monkeypatch):
    import stoa.features.region as R

    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path))
    key = "0123456789abcdef-p0-" + T.ENGINE_V
    d = T.cache_dir() / key
    d.mkdir()
    st = _st()
    st["rows"].append({"section": "room", "item": "PE Store", "copy": 1, "copies": 1, "quantity": "baseboard",
                       "unit": "m", "value": 2.0, "confidence": 0.3, "evidence": []})
    T._write_json(d / "result.json", st)
    seen = {}

    def shape(abs_path, page, space, points, mode="area", snap=False, units_per_m=None):
        seen.update(space=space, upm=units_per_m, snap=snap)
        return {"perimeter_m": 30.0, "skirting_m": 28.2, "polygon": points,
                "openings": [{"a": [0, 0], "b": [1.8, 0], "m": 1.8}]}

    monkeypatch.setattr(R, "measure_shape", shape)
    monkeypatch.setattr(T, "resolve", lambda p: ("/x/plan.pdf", p))
    pe = next(r for r in T.status(key)["rows"] if r["item"] == "PE Store" and r["quantity"] == "area")
    T.fix(key, pe["id"], 50.0, "m2", [[0, 0], [10, 0], [10, 5], [0, 5]], "pdf_pt", "PE Store")
    assert seen == {"space": "pdf_pt", "upm": 1.0, "snap": False}  # 50 units² for 50 m²: the person's own scale
    rows = T.status(key)["rows"]
    sk = next(r for r in rows if r["item"] == "PE Store" and r["quantity"] == "baseboard")
    assert sk["value"] == 28.2 and sk["derived"] and sk["review"] is None and sk["reading"]["value"] == 2.0
    assert "perimeter 30 m minus 1 opening 1.80 m" in sk["method"]
    assert sk["evidence"][0]["polygon"] and sk["evidence"][1]["polyline"] == [[0, 0], [1.8, 0]]
    T.fix(key, pe["id"], None)  # undoing the area takes its skirting back too
    sk = next(r for r in T.status(key)["rows"] if r["item"] == "PE Store" and r["quantity"] == "baseboard")
    assert sk["value"] == 2.0 and not sk.get("derived") and not sk.get("fixed")


def test_rooms_over_the_limit_are_named_not_dropped_in_silence(monkeypatch):
    rooms = [{"name": n, "key": n.lower(), "pt": [i, i], "bbox": [i, i, i + 1, i + 1], "copy": 1, "copies": 1,
              "by_label": False} for i, n in enumerate(["Hall", "Office", "Kitchen"])]
    monkeypatch.setattr(T, "slab_types", lambda *a, **k: [])
    monkeypatch.setattr(T, "area_tags", lambda *a, **k: [])
    monkeypatch.setattr(T, "printed_table", lambda *a, **k: [])
    monkeypatch.setattr(T, "room_labels", lambda pdf, page=0, limit=None: rooms if limit is None else rooms[:limit])
    monkeypatch.setattr(T, "MAX_ROOMS", 2)
    plan = T.plan_items("x.pdf", 0)
    assert [r["name"] for r in plan["rooms"]] == ["Hall", "Office"] and plan["cut"] == ["Kitchen"]
    assert not any(it["item"] == "Kitchen" for it in plan["items"])


def test_a_building_model_gives_rooms_doors_windows_from_the_ifc(tmp_path):
    from stoa.data import data_root

    ifc = data_root() / "data/ifc_models/bsc-esplanades_1807-ep-ar-v18.ifc"
    if not ifc.exists():
        pytest.skip("dataset not here")
    st = T.finish(T.run(str(ifc), "data/ifc_models/bsc-esplanades_1807-ep-ar-v18.ifc", out_dir=tmp_path,
                        log=lambda *a: None))
    rows = {(r["item"], r["quantity"]): r for r in st["rows"]}
    assert rows[("KORIDOR 119 (1.korrus)", "area")]["value"] == pytest.approx(63.84, abs=0.01)  # = the dataset gold
    assert rows[("KORIDOR 119 (1.korrus)", "area")]["evidence"][0]["ifc_guid"]
    assert rows[("Whole model", "doors")]["value"] == 211 and rows[("Whole model", "windows")]["value"] == 297
    assert sum(r["quantity"] == "area" for r in st["rows"]) == 285
    sm = T.summary(st)
    assert sm["scale"]["text"] == "model units" and not sm["review"]
    # floor by floor: rooms and doors per storey = the dataset's per-floor golds
    by = {x["storey"]: x for x in sm["by_storey"]}
    assert [by[f"{i}.korrus"]["rooms"] for i in range(1, 6)] == [50, 60, 60, 60, 55]
    assert [by[f"{i}.korrus"]["doors"] for i in range(1, 6)] == [46, 42, 42, 42, 39]
    assert any(r["section"] == "storey" and r["item"] == "1.korrus" and r["quantity"] == "doors" and r["value"] == 46
               for r in T.sum_rows(st))
    walls = {(r["item"], r["quantity"]): r["value"] for r in st["rows"] if r["section"] == "wall"}
    assert walls[("Välissein VS-01 460 (external)", "wall_length")] == pytest.approx(834.0, abs=0.5)
    assert all(not r.get("review") for r in st["rows"] if r["section"] in ("wall", "slab"))


def test_an_outline_off_the_drawn_walls_goes_to_review():
    st = _st()
    st["rows"][0]["off_wall"] = 0.88  # Laundry's outline runs over blank paper for 88 % of its length
    rows = {(r["item"], r["quantity"]): r for r in T.finish(st)["rows"]}
    assert rows[("Laundry", "area")]["review"].startswith("its outline runs along drawn walls for only 12%")
    st["rows"][0]["off_wall"] = 0.39
    rows = {(r["item"], r["quantity"]): r for r in T.finish(st)["rows"]}
    assert rows[("Laundry", "area")]["review"] is None


def test_a_scanned_pdfs_evidence_goes_back_to_its_page_points():
    rows = [{"item": "Laundry", "label_box": [278, 278, 556, 556],
             "evidence": [{"bbox": [278, 278, 556, 556], "bbox_space": "image_px", "polygon": [[278, 278], [556, 278],
                                                                                              [556, 556]]},
                          {"ifc_guid": "x", "bbox_space": None}]}]
    T.px_to_pt(rows, "upload/abcabcabcabc/scan.pdf", 2.78)  # the render: 2.78 px per pt
    e = rows[0]["evidence"][0]
    assert e["bbox"] == [100.0, 100.0, 200.0, 200.0] and e["bbox_space"] == "pdf_pt" and e["page"] == 0
    assert e["polygon"][1] == [200.0, 100.0] and rows[0]["label_box"] == [100.0, 100.0, 200.0, 200.0]
    assert rows[0]["evidence"][1] == {"ifc_guid": "x", "bbox_space": None}


def test_finnish_room_codes():
    # CubiCasa-style / Finnish housing plans name rooms only by code, often over the room's m² ('OH 16,3')
    assert [T.room_type(n) for n in ("OH", "MH 12,5", "K 10,7", "PESUH.", "S", "ET", "VH 2,1 m2", "KHH", "MH2",
                                     "Bastu")] == \
        ["living", "living", "kitchen", "wet", "wet", "circulation", "storage", "wet", "living", "wet"]
    assert T.fi_code("2H+K+S 55,0 m²") is None  # the flat's type + total, not a room
    assert T.fi_code("OH 16,3") == ("living", "living room")
    # short codes are rooms only on a sheet that prints >= 2 distinctive Finnish codes (grid bubbles H, K, S elsewhere)
    assert T.fi_sheet(["OH 16,3", "K 10,7", "MH 12,5", "ET", "PESUH", "S"])
    assert not T.fi_sheet(["H", "K", "S", "A", "Kitchen"])
    assert T._room_name_ok("K", fi=True) and not T._room_name_ok("K")
    assert T._room_name_ok("Teacher Office") and not T._room_name_ok("2H+K+S 55,0 m²", fi=True)


def test_finnish_printed_area_under_code():
    # X1-style A4 scan, traced: 'OH' over '16.3', 'K' over '10.7' (OCR boxes in pdf_pt)
    lines = [("OH", (203, 299, 221, 316)), ("K", (379, 300, 386, 316)), ("16.3", (204, 313, 228, 330)),
             ("10.7", (380, 313, 405, 330)), ("55.0m²", (359, 459, 416, 484)), ("HH", (203, 552, 220, 569)),
             ("12.5", (203, 564, 228, 581))]
    assert T.fi_printed((203, 299, 221, 316), lines)["value"] == 16.3
    assert T.fi_printed((379, 300, 386, 316), lines)["value"] == 10.7
    assert T.fi_printed((203, 552, 220, 569), lines)["value"] == 12.5
    assert T.fi_printed((376, 503, 394, 522), lines) is None  # 'ET' prints no area
    assert T.fi_name("HH") == "MH" and T.fi_name("OH 16,3") == "OH" and T.fi_name("Kitchen") is None


def test_fi_rows_open_plan_minus_printed():
    # OH + K + ET trace to ONE outline (open plan): OH / K keep their printed m², ET = outline - printed, skirting Review
    poly = [[0, 0], [100, 0], [100, 100], [0, 100]]
    rooms = [{"name": "OH (living room)", "code": "OH", "fi": True, "copy": 1, "pt": [10, 10], "printed":
              {"value": 16.3, "text": "16.3", "bbox": [0, 0, 5, 5]}},
             {"name": "K (kitchen)", "code": "K", "fi": True, "copy": 1, "pt": [60, 10], "printed":
              {"value": 10.7, "text": "10.7", "bbox": [50, 0, 55, 5]}},
             {"name": "ET (entrance hall)", "code": "ET", "fi": True, "copy": 1, "pt": [50, 80]},
             {"name": "MH (bedroom)", "code": "MH", "fi": True, "copy": 1, "pt": [150, 50], "printed":
              {"value": 12.5, "text": "12.5", "bbox": [150, 60, 155, 65]}}]
    rows = [{"section": "room", "item": r["name"], "copy": 1, "quantity": "area", "value": 32.76, "confidence": 0.5,
             "evidence": [{"polygon": poly, "bbox": [0, 0, 100, 100]}], "assumptions": ["scale: 1:75 printed"]}
            for r in rooms[:3]]
    rows.append({"section": "room", "item": "MH (bedroom)", "copy": 1, "quantity": "area", "value": 12.6,
                 "confidence": 0.5, "evidence": [{"polygon": [[120, 0], [200, 0], [200, 100], [120, 100]]}]})
    rows.append({"section": "room", "item": "ET (entrance hall)", "copy": 1, "quantity": "baseboard", "value": 30.0,
                 "confidence": 0.45, "assumptions": []})
    T.fi_rows(rows, rooms, "upload/x/plan.png", 0, {"n": 75.1, "agree": 1, "how": "from ONE printed room area"})
    got = {r["item"]: r for r in rows if r["quantity"] == "area"}
    assert got["OH (living room)"]["value"] == 16.3 and got["OH (living room)"]["confidence"] == 0.85
    assert got["K (kitchen)"]["value"] == 10.7
    assert got["ET (entrance hall)"]["value"] == 5.76 and got["ET (entrance hall)"]["confidence"] <= 0.35
    assert rows[-1]["confidence"] == 0.3  # its skirting ran round the whole open plan
    assert got["MH (bedroom)"]["value"] == 12.5 and got["MH (bedroom)"]["check"]["value"] == 12.6


def test_finnish_codes_pair_with_their_printed_area():
    # cubicasa5k high_quality_architectural-10712 traced (pdf_pt): 'S' with '2.5 m²' up-right, '1200 VH' (a dimension
    # OCR'd into the code's line), 'ET' two lines under the flat's '45.5 m²', lowercase cabinet codes 'sk' / 'hk'
    lines = [("KH", (429, 85, 450, 106)), ("S", (600, 88, 610, 109)), ("2.5 m²", (642, 73, 696, 99)),
             ("3.5 m²", (445, 109, 497, 134)), ("sk", (338, 163, 351, 181)), ("3.0 m²", (584, 199, 641, 227)),
             ("KK", (189, 210, 210, 232)), ("6.0 m²", (239, 203, 297, 231)), ("1200   VH", (513, 220, 563, 245)),
             ("2 H+KK+S", (339, 247, 413, 268)), ("45.5 m²", (370, 269, 431, 294)), ("ET", (398, 303, 418, 325)),
             ("5.5 m²", (409, 335, 465, 362)), ("MH", (578, 351, 601, 372)), ("10.0 m²", (561, 377, 622, 402)),
             ("TK", (420, 523, 441, 546)), ("2.0 m²", (413, 569, 470, 597))]
    labs = T.fi_lines(lines)
    assert [lab["text"] for lab in labs] == ["KH", "S", "KK", "VH", "ET", "MH", "TK"]
    rooms = [{"name": lab["text"], "bbox": lab["bbox"]} for lab in labs]
    T.fi_pair(rooms, lines)
    assert {r["name"]: (r.get("printed") or {}).get("value") for r in rooms} == \
        {"KH": 3.5, "S": 2.5, "KK": 6.0, "VH": 3.0, "ET": 5.5, "MH": 10.0, "TK": 2.0}


def test_not_to_scale_words():
    assert T._NTS_RX.search("SUUNTAA-ANTAVA,EI MITTAKAAVASSA") and T._NTS_RX.search("Not to scale")
    assert not T._NTS_RX.search("DO NOT SCALE")  # the UK title block note: do not measure off the print


def test_bare_wc_is_a_room_name():
    # Esplanades floor 1 prints 'WC' (IfcSpace WC 109) alone: 2 letters, so lineplan.labels skipped it
    assert T._room_name_ok("WC") and T._room_name_ok("W.C.") and not T._room_name_ok("Up")


def _tag_pdf(tmp_path, texts):
    import pymupdf

    p = tmp_path / "sheet.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=1200, height=800)
    for i, t in enumerate(texts):
        pg.insert_text((40 + (i % 8) * 140, 60 + (i // 8) * 40), t, fontsize=9)
    doc.save(p)
    return str(p)


def test_piping_plan_rows_count_tag_families(tmp_path):
    """Stoa's M-101A ask (16:10 'piping plans too'): numbered tags = distinct IDs (a repeat in a note is one item),
    unnumbered symbols (PG) = every printed label; one evidence box per counted tag."""
    texts = ["CV-1", "CV-2", "CV-2", "FP-1", "FP-2", "FP-3", "PG", "PG", "PG", "PG", "PG", "PS-1", "PS-4", "MAV",
             "8\" FROM BULK", "FSI-1", "FSR-1", "FSR-2"]
    pdf = _tag_pdf(tmp_path, texts)
    rows = T.piping_rows(pdf, "upload/x/sheet.pdf", 0)
    got = {r["family"]: r["value"] for r in rows}
    assert got == {"CV": 2, "FP": 3, "FSI": 1, "FSR": 2, "PS-n": 2, "PG": 5, "MAV": 1}
    cv = next(r for r in rows if r["family"] == "CV")
    assert cv["section"] == "equipment" and len(cv["evidence"]) == 2 and cv["evidence"][0]["bbox_space"] == "pdf_pt"
    assert "CV-1, CV-2" in cv["assumptions"][0]
    plan = T.plan_items(pdf, 0)
    assert plan["rooms"] == [] and {it["section"] for it in plan["items"]} == {"equipment"}  # no door / window asks
    assert "piping" in plan["source"]


def test_a_few_tags_are_no_piping_plan(tmp_path):
    pdf = _tag_pdf(tmp_path, ["CV-1", "PG", "Kitchen", "Store"])
    assert T.piping_rows(pdf, "upload/x/sheet.pdf", 0) == []


def test_a_single_reading_trace_says_why_it_waits_in_review(monkeypatch):
    """F2 c9: on a traced scan only one line set is read (support 1 of 1): the room goes to Review AND says why."""
    from stoa.evidence import lineplan
    from stoa.features import takeoff as T

    poly = [[0, 0], [100, 0], [100, 80], [0, 80]]
    monkeypatch.setattr(lineplan, "_measure", lambda *a, **k: {"polygon": poly, "area_m2": 12.5, "support": 1,
                                                               "n_readings": 1, "variant": "dark|doors@200"})
    monkeypatch.setattr(lineplan, "_skirting", lambda *a, **k: None)
    room = {"name": "Group Room", "copy": 1, "copies": 2, "bbox": [10, 10, 40, 20], "pt": [25, 15]}
    rows = T.measure_copy("x.pdf", "upload/x.pdf", 0, room, 200.0)
    area = next(r for r in rows if r["quantity"] == "area")
    assert area["confidence"] == 0.35 and "1 of 1 readings" in area["rough"]
    why = T.review_reason(area)
    assert why.startswith("rough reading (confidence 0.35): 1 of 1 readings")
    monkeypatch.setattr(lineplan, "_measure", lambda *a, **k: {"polygon": poly, "area_m2": 12.5, "support": 4,
                                                               "n_readings": 5, "variant": "dark"})
    ok = next(r for r in T.measure_copy("x.pdf", "upload/x.pdf", 0, room, 200.0) if r["quantity"] == "area")
    assert "rough" not in ok and T.review_reason(ok) is None
