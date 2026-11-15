"""F3 order list (stoa.features.orderlist): skirting pieces, doors by size, floor covering by room type, from F2's
takeoff rows. Synthetic rows / sheets only (no takeoff job, no model)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from stoa.features import orderlist as OL


def _row(item, q, v, unit, conf=0.6, copy=1, copies=1, **kw):
    return {"section": "room", "item": item, "copy": copy, "copies": copies, "quantity": q, "value": v, "unit": unit,
            "confidence": conf, "status": "ok", "method": "lineplan", "evidence": [{"file": "f.pdf", "page": 0,
                                                                                    "bbox": [0, 0, 10, 10]}], **kw}


ROWS = [
    _row("Bedroom 1", "area", 12.0, "m2"), _row("Bedroom 1", "baseboard", 13.1, "m"),
    _row("Bedroom 2", "area", 10.0, "m2"), _row("Bedroom 2", "baseboard", 2.0, "m"),
    _row("KPH", "area", 4.0, "m2"), _row("KPH", "baseboard", 7.5, "m"),  # tiled wet room: no skirting
    _row("Lift", "area", 3.0, "m2"),  # no floor covering, no skirting
    _row("Lift Landing", "area", 6.0, "m2"), _row("Lift Landing", "baseboard", 5.0, "m"),
    _row("Living (2.05)", "area", 20.0, "m2"), _row("Living (2.05)", "baseboard", 1, "count"),  # not a length
    _row("Store", "area", 2.0, "m2", conf=0.1), _row("Store", "baseboard", 4.0, "m", conf=0.1),  # rough -> review
    _row("Office", "area", 100.0, "ft2"), _row("Office", "baseboard", 40.0, "ft"),
    {"section": "total", "item": "Sum of room areas", "quantity": "area_sum", "value": 999, "unit": "m2"},
]


def test_skirting_pieces_per_room_with_waste_and_review_list():
    ol = OL.build({"rows": ROWS, "file": "f.pdf", "page": 0})
    sk = ol["skirting"]
    by = {x["room"]: x for x in sk["lines"]}
    # 13.1 m + 10 % = 14.41 m -> 7 pieces of 2.4 m (6.004 -> 7); 2.0 m + 10 % = 2.2 -> 1 piece
    assert by["Bedroom 1"]["pieces"] == 7 and by["Bedroom 2"]["pieces"] == 1
    assert by["Lift Landing"]["pieces"] == 3  # a landing is an ordinary floor
    assert by["Office"]["net_m"] == pytest.approx(12.19, abs=0.01)  # 40 ft
    assert "KPH" in {x["room"] for x in sk["excluded"]} and "Lift" not in by
    assert {x["room"] for x in sk["review"]} == {"Living (2.05)", "Store"}  # a count / a rough reading: not in totals
    assert sk["pieces"] == sum(x["pieces"] for x in sk["lines"])
    assert sk["total_net_m"] == pytest.approx(13.1 + 2.0 + 5.0 + 12.192, abs=0.02)
    assert set(ol["defaults"]) == set(OL.DEFAULTS)  # every option is still the labelled default
    assert "room(s) to check by hand are NOT included" in OL.answer_text(ol)


def test_options_change_waste_and_stock_length():
    ol = OL.build({"rows": ROWS}, {"skirting_waste_pct": 0, "stock_m": 3.0, "floor_waste_pct": 5})
    by = {x["room"]: x for x in ol["skirting"]["lines"]}
    assert by["Bedroom 1"]["pieces"] == 5  # 13.1 / 3.0 = 4.37 -> 5
    assert "skirting_waste_pct" not in ol["defaults"] and "stock_m" not in ol["defaults"]
    assert ol["floor"]["total_with_waste_m2"] == pytest.approx(ol["floor"]["total_m2"] * 1.05, abs=0.02)
    bad, d = OL.opts_of({"stock_m": "abc", "skirting_waste_pct": -3})
    assert bad["stock_m"] == 2.4 and "stock_m" in d and "skirting_waste_pct" in d


def test_floor_covering_by_room_type():
    fl = OL.build({"rows": ROWS})["floor"]
    by = {g["type"]: g for g in fl["lines"]}
    assert by["Bedroom"]["m2"] == pytest.approx(22.0) and len(by["Bedroom"]["rooms"]) == 2
    assert by["Office"]["m2"] == pytest.approx(9.29, abs=0.01)  # 100 ft²
    assert "Lift" not in by and "Lift Landing" in by
    assert by["KPH"]["wet"] is True
    assert "Store" in {x["room"] for x in fl["review"]}


@pytest.mark.parametrize("name, want", [("Zimmer (2.01)", "Zimmer"), ("Bedroom 2", "Bedroom"), ("Group Room", "Group Room"),
                                        ("Whg. A Wohnen/Essen (2.05)", "Wohnen/Essen"), ("MH 3", "MH"),
                                        ("Office 104B", "Office")])
def test_room_type(name, want):
    assert OL.room_type(name) == want


def test_printed_door_size_tags(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    for i, t in enumerate(["9x21", "9x21", "10x21", "15x21", "9x21", "1:100", "2x3", "120x210"]):
        pg.insert_text((40 + 60 * i, 100), t, fontsize=8)
    p = tmp_path / "plan.pdf"
    doc.save(p)
    d = OL.door_sizes(str(p), 0, 100.0, "upload/x/plan.pdf")
    assert d["source"] == "printed size tags"
    by = {g["size"]: g for g in d["lines"]}
    assert by["9x21"]["sets"] == 3 and by["10x21"]["sets"] == 1 and by["15x21"]["kind"] == "pair"
    assert d["sets"] == 5 and d["leaves"] == 6
    assert by["9x21"]["evidence"][0]["file"] == "upload/x/plan.pdf"


def test_exports_csv_and_xlsx():
    ol = OL.build({"rows": ROWS, "file": "f.pdf"})
    csv = OL.to_csv(ol)
    assert csv.splitlines()[0].startswith("Section,Item,Quantity") and "Check by hand" in csv and "(default)" in csv
    x = OL.to_xlsx(ol)
    with zipfile.ZipFile(io.BytesIO(x)) as z:
        s1 = z.read("xl/worksheets/sheet1.xml").decode()
        wb = z.read("xl/workbook.xml").decode()
    assert "Skirting" in s1 and "Bedroom 1" in s1 and "Order list" in wb


def test_router_is_off_unless_named(monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "selfcheck")
    assert not OL.enabled()
    monkeypatch.setenv("STOA_FEATURES", "selfcheck,orderlist")
    assert OL.enabled()
    monkeypatch.setenv("STOA_FEATURES", "all")
    assert OL.enabled()


@pytest.mark.parametrize("name, wet", [("TUALETTRUUM", True), ("INVA-WC", True), ("KPH", True), ("Pesuhuone", True),
                                       ("Accessible Toilet", True), ("Laundry", True), ("ELUTUBA+KÖÖK", False),
                                       ("Bedroom", False), ("Group Room", False), ("Lift Landing", False)])
def test_which_rooms_get_skirting(name, wet):
    why = OL.no_skirting(name)
    assert (why is not None and "wet" in why) == wet
    if name == "Lift Landing":
        assert why is None and OL.no_floor(name) is None
    assert OL.no_floor("Lift") and OL.no_skirting("Plant Room") and "plant" in OL.no_skirting("Plant Room")


def test_doors_from_the_linked_ifc_model_are_exact():
    from stoa.data import data_root

    p = data_root() / "data" / "ifc_models" / "bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"
    if not p.exists():
        pytest.skip("ifc_models plan not on this machine")
    d = OL.door_sizes(str(p), 0, 100.0, "data/ifc_models/" + p.name)
    assert d["source"].startswith("3D model") and d["sets"] == 46
    by = {(g["kind"], g["size"]): g for g in d["lines"]}
    assert by[("single", "1000 × 2100 mm")]["sets"] == 30  # = the model's 'Door 22' OverallWidth 1.0 m
    assert all(g["leaves"] == 2 * g["sets"] for g in d["lines"] if g["kind"] == "pair")
    assert all(e["ifc_guid"] for g in d["lines"] for e in g["evidence"])


@pytest.mark.parametrize("text, want", [
    ("101A F 915 2100 10 SP A PS S-302", (915, 2100, False, "915 × 2100 mm")),          # AW-33 (metric, mm)
    ("""104 PR 3'-0" 7'-0" 1 3/4" HM""", (914, 2134, True, "3'-0\" × 7'-0\"")),        # a pair, feet-inches
    ("""102S 2' - 10" 7' - 0" 1 3/4" N HM""", (864, 2134, False, "2'-10\" × 7'-0\"")),  # AW-28 spacing
    ("110A F 1800 2150 44 HM", (1800, 2150, True, "1800 × 2150 mm")),                     # >= 1.5 m wide = double
    ("ES101 ELECTRICAL SITE PLAN", None), ("12 1:100 2021", None)])
def test_door_schedule_row_sizes(text, want):
    assert OL._row_size(text) == want


def test_door_schedule_on_stoa_sheet():
    p = Path(".stoa/datasets/stoa-like/originals/AW_078-24-33.p76.pdf")
    if not p.exists():
        pytest.skip("stoa-like copy not on this machine")
    d = OL.door_sizes(str(p), 0, None, "upload/x/" + p.name)
    assert d["source"].startswith("door schedule") and d["sets"] == 8 and d["leaves"] == 9
    by = {g["size"]: g for g in d["lines"]}
    assert by["915 × 2100 mm"]["sets"] == 7 and by["1800 × 2150 mm"]["kind"] == "pair"


def test_picture_doors_never_trace_on_request(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    img = tmp_path / "plan.png"
    pil.new("RGB", (400, 300), "white").save(img)
    d = OL.door_sizes(str(img), 0, 100.0, "upload/x/plan.png")
    assert d["source"] is None and d["sets"] == 0 and "no traced copy yet" in d["notes"][0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["plan.png"]  # nothing written next to the picture


def test_takeoff_review_and_wet_fields_are_honoured():
    rows = [_row("Hall", "area", 50.0, "m2", room_type="hall"), _row("Hall", "baseboard", 3.0, "m",
                                                                     review="implausible: under 35 % of 4·√A"),
            _row("Shower Block", "area", 9.0, "m2", wet=True), _row("Shower Block", "baseboard", 11.0, "m", wet=True),
            _row("Room X", "area", 12.0, "m2", wet=True), _row("Room X", "baseboard", 13.0, "m", wet=True)]
    ol = OL.build({"rows": rows})
    sk = ol["skirting"]
    assert [x["room"] for x in sk["review"]] == ["Hall"] and "implausible" in sk["review"][0]["why"]
    assert {x["room"] for x in sk["excluded"]} == {"Shower Block", "Room X"} and not sk["lines"]
    g = {x["type"]: x for x in ol["floor"]["lines"]}
    assert g["Hall"]["category"] and g["Hall"]["m2"] == 50.0
    # the wet flag follows F2's room_type KEY (its labels were renamed at 00:11: 'Toilets & wet rooms' -> 'Wet rooms')
    fl = OL.build({"rows": [_row("Room Y", "area", 6.0, "m2", room_type="wet")]})["floor"]["lines"]
    assert fl[0]["wet"] and "wet_key" not in fl[0]


def test_wet_room_skirting_switch():
    on = OL.build({"rows": ROWS}, {"wet_skirting": 1})
    assert "KPH" in {x["room"] for x in on["skirting"]["lines"]} and "wet_skirting" not in on["defaults"]
    off = OL.build({"rows": ROWS})
    assert "KPH" not in {x["room"] for x in off["skirting"]["lines"]} and "wet_skirting" in off["defaults"]
    assert "Lift" not in {x["room"] for x in on["skirting"]["lines"]}  # the switch is about wet rooms only
    kph = next(x for x in on["skirting"]["lines"] if x["room"] == "KPH")
    assert kph["wet"] is True and not any(x["wet"] for x in on["skirting"]["lines"] if x["room"] != "KPH")


def test_openings_not_deducted_uses_the_takeoffs_whole_outline():
    comp = {"perimeter_m": 16.0, "skirting_net_m": 13.1}
    rows = [_row("Bedroom 1", "area", 12.0, "m2"), _row("Bedroom 1", "baseboard", 13.1, "m", components=comp),
            _row("Bedroom 2", "area", 10.0, "m2"), _row("Bedroom 2", "baseboard", 9.0, "m")]  # no components
    d = OL.build({"rows": rows})
    assert d["skirting"]["total_net_m"] == pytest.approx(22.1) and "deduct_openings" in d["defaults"]
    g = OL.build({"rows": rows}, {"deduct_openings": 0})
    by = {x["room"]: x for x in g["skirting"]["lines"]}
    assert by["Bedroom 1"]["net_m"] == 16.0 and by["Bedroom 2"]["net_m"] == 9.0  # kept net: no outline given
    assert g["skirting"]["no_gross"] == 1 and "NOT deducted" in g["skirting"]["rule"]
    assert "deduct_openings" not in g["defaults"]
    assert OL.opts_of({"deduct_openings": 1})[1].count("deduct_openings") == 1  # 1 = the default


def test_peek_finds_an_existing_takeoff_and_never_starts_one(tmp_path, monkeypatch):
    import json

    from stoa.features import takeoff as T

    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path / "up"))
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tmp_path / "cache"))
    f = tmp_path / "up" / "0123456789ab" / "plan.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF-1.4\n% plan\n")
    v = "upload/0123456789ab/plan.pdf"
    assert OL.peek(v, 0) == {"status": "none"}
    assert not any((tmp_path / "cache").iterdir())  # nothing started
    key = T.job_key(str(f.resolve()), 0)
    d = T.cache_dir() / key
    d.mkdir(parents=True)
    (d / "progress.json").write_text(json.dumps({"status": "running", "file": v, "page": 0, "done": 3, "total": 9}))
    st = OL.peek(v, 0)
    assert st["status"] == "running" and st["job"] == key


def test_cased_openings_in_a_door_schedule_are_not_ordered(tmp_path, monkeypatch):
    """F8 23:04: xref.schedule_rows marks CASED OPENING rows opening_only (no door leaf to order)."""
    import pymupdf

    from stoa.features import xref

    pdf = tmp_path / "sched.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    rows = []
    for i, (mark, size, only) in enumerate([("101", "915 2100", False), ("102", "915 2100", False),
                                            ("103", "1830 2100", False), ("104", "1200 2100", True)]):
        y = 50 + 30 * i
        pg.insert_text((40, y), f"{mark}   {size}", fontsize=9)
        rows.append({"kind": "door", "table": "DOOR SCHEDULE", "key": mark, "bbox": [30, y - 12, 300, y + 4],
                     "opening_only": only})
    doc.save(pdf)
    doc.close()
    OL._schedule_pages.cache_clear() if hasattr(OL._schedule_pages, "cache_clear") else None
    monkeypatch.setattr(OL, "_schedule_pages", lambda *a, **k: [0])
    monkeypatch.setattr(xref, "schedule_rows", lambda p, page: rows)
    got = OL.schedule_doors(str(pdf), 0, "sched.pdf")
    assert got is not None and got["sets"] == 3  # 104 is a cased opening: nothing to order
    assert "1 cased opening without a door" in got["notes"][0]


def test_revit_door_schedule_one_word_per_cell(tmp_path, monkeypatch):
    """A Revit schedule prints each cell as its own word (Sports Complex A-801): when xref's rows carry no size (its
    door-type legend 'WD-1 STL-1 A 15'), the door-number words ('103.A') and their visual rows are read instead."""
    import pymupdf

    from stoa.features import xref

    pdf = tmp_path / "a801.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=900, height=400)
    pg.insert_text((40, 30), "Door Schedule", fontsize=10)
    cells = [("103.A", "OFFICE", "103", "2' - 8\"", "7' - 0\"", "WD-1"),
             ("100.A1", "VEST.", "100", "2' - 11 3/16\"", "8' - 0\"", "AL-2"),
             ("100.A2", "VEST.", "100", "2' - 11 3/16\"", "8' - 0\"", "AL-2"),
             ("130.A", "STORAGE", "130", "8' - 0\"", "8' - 0\"", "HM-1")]
    for i, row in enumerate(cells):
        x, y = 40, 60 + 22 * i
        for c in row:
            pg.insert_text((x, y), c, fontsize=8)
            x += 90
    pg.insert_text((700, 60), "GENERAL NOTE 12' - 0\" 9' - 0\"", fontsize=8)  # a note on the same visual row: cut
    doc.save(pdf)
    doc.close()
    monkeypatch.setattr(OL, "_schedule_pages", lambda *a, **k: [0])
    monkeypatch.setattr(xref, "schedule_rows", lambda p, page: [
        {"kind": "door", "table": "KEYNOTE - DOOR SCHEDULE", "key": "WD-1", "bbox": [0, 0, 10, 10]},
        {"kind": "door", "table": "KEYNOTE - DOOR SCHEDULE", "key": "AL-2", "bbox": [0, 20, 10, 30]}])
    got = OL.schedule_doors(str(pdf), 0, "a801.pdf")
    assert got is not None and got["sets"] == 4 and got["leaves"] == 5  # the 8'-0" wide door is a pair
    sizes = {x["size"]: x["sets"] for x in got["lines"]}
    assert sizes == {"2'-8\" × 7'-0\"": 1, "2'-11 3/16\" × 8'-0\"": 2, "8'-0\" × 8'-0\"": 1}


def test_door_schedule_on_sports_complex_a801():
    from stoa.data import abspath

    p = abspath("data/opening_design/Sports_Complex/Sheets - Current Set/A-801 - Door and Finish Schedules & Wall Types.pdf")
    if not p.is_file():
        pytest.skip("needs the dataset (STOA_DATA)")
    got = OL.schedule_doors(str(p), 0, "A-801.pdf")
    assert got is not None and got["sets"] == 37 and got["leaves"] == 41
    assert "38 marks, 1 without a readable size" in got["notes"][0]  # 128.B: 12'-0" x 14'-0" overhead door


def test_printed_doors_need_no_takeoff(tmp_path, monkeypatch):
    """GET /feat/orderlist/doors: a door schedule answers 'how many doors to order' without a room takeoff; a plan with
    no printed source gives nothing (swings are measured only with the takeoff's scale)."""
    import pymupdf

    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    d = tmp_path / "0123456789ab"
    d.mkdir()
    doc = pymupdf.open()
    doc.new_page(width=300, height=200).insert_text((20, 40), "GROUND FLOOR PLAN", fontsize=9)
    doc.save(d / "plan.pdf")
    doc.close()
    called = []
    monkeypatch.setattr(OL, "measured_doors", lambda *a, **k: called.append(1) or ([], []))
    got = OL.printed_doors("upload/0123456789ab/plan.pdf", 0)
    assert got["sets"] == 0 and got["source"] is None and not called  # never measures swings


@pytest.mark.parametrize("name, odd", [("Enclosed Wall", True), ("Boundary Fence", True), ("Wall", True),
                                       ("Enclosed Walkway", False), ("Wall Store", False), ("Group Room", False),
                                       ("Zimmer (2.01)", False), ("Screen Room", False)])
def test_a_label_naming_a_building_element_is_no_room(name, odd):
    assert bool(OL.not_a_room(name)) == odd


def test_misread_room_goes_to_check_by_hand_not_the_order():
    rows = [{"section": "room", "item": "Enclosed Wall", "quantity": "baseboard", "unit": "m", "value": 64.6,
             "confidence": 0.45},
            {"section": "room", "item": "Enclosed Wall", "quantity": "area", "unit": "m2", "value": 119.4,
             "confidence": 0.5},
            {"section": "room", "item": "Group Room", "quantity": "baseboard", "unit": "m", "value": 20.0,
             "confidence": 0.5}]
    o, _ = OL.opts_of({})
    sk = OL.skirting(rows, o)
    assert [x["room"] for x in sk["lines"]] == ["Group Room"] and sk["review"][0]["room"] == "Enclosed Wall"
    fl = OL.floor_covering(rows, o)
    assert fl["total_m2"] == 0 and "building element" in fl["review"][0]["why"]


def test_scanned_pdf_doors_come_from_its_pictures_traced_copy(monkeypatch):
    """A scanned page has no lines: doors come from picture_doors on its render (F2's takeoff traces that render),
    boxes mapped from image px back to the PDF page's points."""
    seen = {}

    def fake_picture_doors(img, scale_n, file):
        seen["img"] = img
        return {"source": "door swings on the picture's traced lines", "confidence": 0.45, "sets": 1, "leaves": 2,
                "notes": [], "lines": [{"size": "double door", "kind": "pair", "sets": 1, "leaves": 2, "evidence": [
                    {"file": file, "page": 0, "bbox": [100.0, 200.0, 300.0, 400.0], "bbox_space": "image_px"}]}]}

    monkeypatch.setattr(OL, "_scan", lambda pdf, page: ("/x/set.p3.scan.png", 4.0))
    monkeypatch.setattr(OL, "picture_doors", fake_picture_doors)
    monkeypatch.setattr(OL, "schedule_doors", lambda *a, **k: None)
    monkeypatch.setattr(OL, "printed_size_tags", lambda *a, **k: [])
    d = OL.door_sizes("/x/set.pdf", 2, 200.0, "upload/abc/set.pdf")
    e = d["lines"][0]["evidence"][0]
    assert seen["img"] == "/x/set.p3.scan.png" and d["sets"] == 1 and "scanned PDF" in d["notes"][0]
    assert (e["bbox"], e["bbox_space"], e["page"]) == ([25.0, 50.0, 75.0, 100.0], "pdf_pt", 2)


def test_the_visitors_own_takeoff_fixes_are_asked_for(monkeypatch):
    """F2 keeps Measure fixes per visitor (night-F2 1ef28b65): the order list passes the browser id on; an older takeoff
    without the parameter still works; a malformed id is dropped."""
    calls = []

    class NewT:
        @staticmethod
        def status(key, virtual=None, visitor=None):
            calls.append(("new", key, visitor))
            return {"status": "running"}

    class OldT:
        @staticmethod
        def status(key, virtual=None):
            calls.append(("old", key))
            return {"status": "running"}

    assert OL._status(NewT, "j1", None, "abcdef1234567890")["status"] == "running"
    assert OL._status(OldT, "j2", None, "abcdef1234567890")["status"] == "running"
    assert calls == [("new", "j1", "abcdef1234567890"), ("old", "j2")]
    assert OL._visitor("abcdef1234567890") and OL._visitor("../etc") is None and OL._visitor("") is None


def _node_ok() -> str | None:
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


@pytest.mark.skipif(_node_ok() is None, reason="needs Node >= 22.18 (type stripping)")
def test_orderlist_question_rules_node_tests():
    """Which typed questions the order list answers + the stock length / waste a question names
    (web/src/features/orderlist/question.ts)."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    test = root / "web" / "src" / "features" / "orderlist" / "question.test.ts"
    r = subprocess.run([_node_ok(), "--test", str(test)], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ℹ fail 0" in r.stdout
