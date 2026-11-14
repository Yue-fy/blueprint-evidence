"""F8 cross-references: sheet index, callouts / notes, hops (stoa/features/xref.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from stoa.features import xref  # noqa: E402

AW33 = Path(".stoa/datasets/stoa-like/originals/AW_078-24-33.pdf")


def _set(tmp_path: Path) -> str:
    """A 3-sheet set: A-101 floor plan (callout '3' over 'A-501', 'SEE SHEET A-601 FOR DOOR SCHEDULE', 'SEE STRUCT.
    SHTS.'), A-501 sections (view '3' 'WALL SECTION'), A-601 door schedule; plus S-101 for the discipline ref."""
    doc = pymupdf.open()
    sheets = [("A-101", "FLOOR PLAN"), ("A-501", "WALL SECTIONS"), ("A-601", "DOOR SCHEDULE"), ("S-101", "FOUNDATION PLAN")]
    for num, title in sheets:
        pg = doc.new_page(width=1224, height=792)
        pg.insert_text((1100, 600), title, fontsize=10)          # title-block column, above the number
        pg.insert_text((1100, 760), num, fontsize=24)            # sheet number, bottom right
        if num == "A-101":
            pg.insert_text((400, 300), "3", fontsize=8)              # bubble: view id over the sheet number
            pg.insert_text((393, 312), "A-501", fontsize=8)
            pg.insert_text((100, 500), "SEE SHEET A-601 FOR DOOR SCHEDULE", fontsize=8)
            pg.insert_text((100, 520), "SEE STRUCT. SHTS.", fontsize=8)
            pg.insert_text((100, 540), "SEE A-999 FOR NOTHING", fontsize=8)
            pg.insert_text((600, 300), "12", fontsize=8)                # a door tag
        if num == "A-601":
            pg.insert_text((100, 180), "DOOR SCHEDULE", fontsize=9)     # a schedule table: row keys + values
            for i, k in enumerate(["11", "12", "13"]):
                pg.insert_text((100, 200 + 12 * i), k, fontsize=8)
                pg.insert_text((160, 200 + 12 * i), "915", fontsize=8)
                pg.insert_text((220, 200 + 12 * i), "HM", fontsize=8)
        if num == "A-501":
            pg.insert_text((200, 400), "3", fontsize=20)              # the view's title bubble
            pg.insert_text((230, 400), "WALL SECTION", fontsize=20)
            for i in range(12):                                       # small print around it (dimensions, notes)
                pg.insert_text((200 + 40 * i, 200), f"{100 + i}", fontsize=7)
    p = tmp_path / "set.pdf"
    doc.save(p)
    return str(p)


def test_sheet_index_numbers_and_titles(tmp_path):
    idx = xref.sheet_index(_set(tmp_path))
    assert [e.number for e in idx] == ["A-101", "A-501", "A-601", "S-101"]
    assert idx[2].title == "DOOR SCHEDULE"


def test_page_refs_callout_note_discipline(tmp_path):
    p = _set(tmp_path)
    refs = xref.page_refs(p, 0)
    kinds = {(r.kind, r.sheet, r.detail) for r in refs}
    assert ("callout", "A-501", "3") in kinds
    assert ("note", "A-601", None) in kinds
    call = next(r for r in refs if r.kind == "callout")
    assert call.target_page == 1 and call.target_view == "WALL SECTION" and call.target_bbox
    note = next(r for r in refs if r.kind == "note" and r.sheet == "A-601")
    assert note.target_page == 2 and note.subject == "DOOR SCHEDULE"
    missing = next(r for r in refs if r.sheet == "A-999")
    assert missing.target_page is None and "not in this upload" in missing.note
    disc = next(r for r in refs if r.kind == "discipline")
    assert [t["number"] for t in disc.targets] == ["S-101"]
    # the title-block number is never a reference to itself
    assert not any(r.sheet == "A-101" for r in refs)


def test_follow_plan(tmp_path):
    p = _set(tmp_path)
    h = xref.follow_plan(p, 0, "What is the hardware of door 12?")
    assert h and h.to_page == 2 and h.mode == "auto" and "schedule" in h.why
    h = xref.follow_plan(p, 0, "What is the insulation in section 3?")
    assert h and h.to_page == 1 and h.question.endswith("(view: WALL SECTION)")
    h = xref.follow_plan(p, 0, "What is drawn on A-501?")
    assert h and h.to_page == 1
    # plan questions stay on the plan; a one-page file never hops
    assert xref.follow_plan(p, 0, "How many doors are there?") is None
    assert xref.follow_plan(p, 0, "What is the area of the wall section?") is None


def test_lookup_suffix_rule():
    idx = [xref.SheetEntry(0, "S-100F", "PLAN"), xref.SheetEntry(1, "S-501F", "T"), xref.SheetEntry(2, "S-501P", "T")]
    e, note = xref.lookup("S-501", idx, own="S-100F")
    assert e.number == "S-501F" and "same suffix" in note
    e, note = xref.lookup("S-777", idx, own="S-100F")
    assert e is None


def test_images_and_bad_files_fail_closed(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n")
    assert xref.sheet_index(str(img)) == [] and xref.page_refs(str(img), 0) == []
    assert xref.follow_plan(str(img), 0, "door width?") is None


def test_router_flag_and_paths(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    up = tmp_path / "uploads"
    (up / "abc123").mkdir(parents=True)
    src = _set(tmp_path)
    os.replace(src, up / "abc123" / "set.pdf")
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    app = FastAPI()
    app.include_router(xref.router)
    c = TestClient(app)
    monkeypatch.setenv("STOA_FEATURES", "")
    assert c.get("/feat/xref/sheets", params={"path": "upload/abc123/set.pdf"}).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "batch,xref")
    r = c.get("/feat/xref/sheets", params={"path": "upload/abc123/set.pdf"}).json()
    assert [s["number"] for s in r["sheets"]][:2] == ["A-101", "A-501"]
    r = c.get("/feat/xref/refs", params={"path": "upload/abc123/set.pdf", "page": 1}).json()
    assert r["sheet"]["number"] == "A-501" and r["incoming"][0]["number"] == "A-101"
    r = c.get("/feat/xref/refs", params={"path": "upload/abc123/set.pdf", "page": 1, "incoming": 0}).json()
    assert r["incoming"] is None and r["sheet"]["number"] == "A-501"    # boxes first, the whole-set scan later
    r = c.post("/feat/xref/follow", json={"path": "upload/abc123/set.pdf", "page": 0,
                                          "question": "What is the hardware of door 12?"}).json()
    assert r["hop"]["to_sheet"] == "A-601" and r["trace"][0].startswith("xref: ")
    assert c.get("/feat/xref/sheets", params={"path": "upload/../etc/passwd"}).status_code == 400
    assert c.get("/feat/xref/sheets", params={"path": "/etc/passwd"}).status_code == 400


@pytest.mark.skipif(not AW33.is_file(), reason="USACE AW 078-24-33 set not on this machine")
def test_usace_set_s100f_callouts_resolve_to_views():
    idx = xref.sheet_index(str(AW33))
    assert len(idx) == 121 and all(e.number for e in idx)
    assert idx[55].number == "S-100F" and idx[75].title == "DOOR AND ROOM SCHEDULE"
    refs = xref.page_refs(str(AW33), 55)
    a6 = next(r for r in refs if r.kind == "callout" and r.detail == "A6")
    assert a6.target_sheet == "S-500F" and a6.target_view == "FOUNDATION SECTION 5"
    note = next(r for r in refs if r.kind == "note" and r.sheet == "S-501")
    assert note.target_sheet == "S-501F" and "same suffix" in note.note
    h = xref.follow_plan(str(AW33), 68, "What is the width of door 102?")
    assert h.to_sheet == "A-601" and h.mode == "auto"
    h = xref.follow_plan(str(AW33), 55, "How deep is the pipe trench?")
    assert h.to_sheet == "S-501F" and h.mode == "suggest" and "PIPE TRENCH" in h.question


def _split(tmp_path: Path) -> list[str]:
    """The same 4 sheets as 4 one-page PDFs in one folder (files dropped together)."""
    src = pymupdf.open(_set(tmp_path))
    folder = tmp_path / "up" / "set1"
    folder.mkdir(parents=True)
    out = []
    for i, name in enumerate(["A-101", "A-501", "A-601", "S-101"]):
        d = pymupdf.open()
        d.insert_pdf(src, from_page=i, to_page=i)
        p = folder / f"{name}.pdf"
        d.save(p)
        out.append(str(p))
    return out


def test_refs_and_hops_across_files(tmp_path):
    files = _split(tmp_path)
    plan = files[0]
    alone = xref.page_refs(plan, 0)
    assert all(r.target_page is None for r in alone if r.kind != "discipline")    # one file: nothing to jump to
    refs = xref.page_refs(plan, 0, set_paths=files)
    call = next(r for r in refs if r.kind == "callout")
    assert call.target_file == files[1] and call.target_page == 0 and call.target_view == "WALL SECTION"
    disc = next(r for r in refs if r.kind == "discipline")
    assert disc.targets[0]["file"] == files[3]
    h = xref.follow_plan(plan, 0, "What is the hardware of door 12?", set_paths=files)
    assert h and h.to_file == files[2] and h.to_page == 0 and h.mode == "auto"
    assert xref.follow_plan(plan, 0, "What is the hardware of door 12?") is None   # a lone sheet never hops


def test_router_upload_folder_is_a_set(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _split(tmp_path)
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path / "up"))
    monkeypatch.setenv("STOA_FEATURES", "xref")
    app = FastAPI()
    app.include_router(xref.router)
    c = TestClient(app)
    r = c.get("/feat/xref/sheets", params={"path": "upload/set1/A-101.pdf"}).json()
    assert r["files"] == 4 and {s["file"] for s in r["sheets"]} >= {"upload/set1/A-501.pdf", "upload/set1/A-101.pdf"}
    r = c.get("/feat/xref/refs", params={"path": "upload/set1/A-101.pdf", "page": 0}).json()
    call = next(x for x in r["refs"] if x["kind"] == "callout")
    assert call["target_file"] == "upload/set1/A-501.pdf" and call["target_view"] == "WALL SECTION"
    r = c.get("/feat/xref/refs", params={"path": "upload/set1/A-501.pdf", "page": 0}).json()
    assert r["incoming"] and r["incoming"][0]["file"] == "upload/set1/A-101.pdf"
    r = c.post("/feat/xref/follow", json={"path": "upload/set1/A-101.pdf", "page": 0,
                                          "question": "What is the hardware of door 12?"}).json()
    assert r["hop"]["to_file"] == "upload/set1/A-601.pdf"


def test_image_refs_from_the_cached_text_layer_only(tmp_path, monkeypatch):
    """A picture of a plan: references come from the text layer the upload already read (never a cold read); tile
    duplicates keep the reading the sheet prints more often; a set of picture + PDFs resolves into the PDF."""
    import stoa.solvers.raster.sheet as rs
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    files = _split(tmp_path)
    img = tmp_path / "up" / "set1" / "A-101-scan.png"
    img.write_bytes(b"\x89PNG\r\n")
    items = [TextItem("FLOOR PLAN", [5000, 3000, 5600, 3050]), TextItem("A-101", [5000, 3700, 5500, 3800]),
             TextItem("3", [1990, 1430, 2010, 1470]), TextItem("A-501", [1960, 1480, 2050, 1520]),
             TextItem("A-50I", [1961, 1481, 2049, 1519], tile=1), TextItem("A-501", [3000, 2000, 3090, 2040]),
             TextItem("SEE SHEET A-601 FOR DOOR SCHEDULE", [400, 2400, 1400, 2440])]
    layer = TextLayer(path=str(img), size=(6000, 4000), items=items)
    monkeypatch.setattr(rs, "cached_layer", lambda p: layer if p == str(img) else None)
    idx = xref.sheet_index(str(img))
    assert idx[0].number == "A-101" and idx[0].title == "FLOOR PLAN"
    refs = xref.page_refs(str(img), 0, set_paths=[str(img), *files])
    call = next(r for r in refs if r.kind == "callout" and r.detail == "3")
    assert call.sheet == "A-501" and call.target_file == files[1] and call.target_view == "WALL SECTION"
    assert not any(r.sheet == "A-50I" for r in refs)
    assert xref.follow_plan(str(img), 0, "What hardware does door 7 have?", set_paths=[str(img), *files]).to_file == files[2]
    monkeypatch.setattr(rs, "cached_layer", lambda p: None)          # not read yet: nothing, and nothing remembered
    other = tmp_path / "up" / "set1" / "B.png"
    other.write_bytes(b"\x89PNG\r\n")
    assert xref.page_refs(str(other), 0) == [] and xref.sheet_index(str(other)) == []


def test_schedule_rows_and_door_tags(tmp_path):
    p = _set(tmp_path)
    rows = xref.schedule_rows(p, 2)
    assert [(r["key"], r["kind"]) for r in rows] == [("11", "door"), ("12", "door"), ("13", "door")]
    tag = next(r for r in xref.page_refs(p, 0) if r.kind == "tag")
    assert tag.text == "12" and tag.target_page == 2 and tag.target_bbox and "door 12" in tag.target_view
    h = xref.follow_plan(p, 0, "What is the hardware of door 12?")
    assert h.to_page == 2 and h.to_bbox == rows[1]["bbox"] and "row 12" in h.why


# ---------------------------------------------------------------------------------------------- copies and floors (cycle 2)
DFE = Path(".stoa/datasets/stoa-like/dfe")
DFE_SCAN = Path(".stoa/neutral/x1scan/b22eef0132/b22eef0132.png")   # X1's 200-dpi scan of DfE L00


def _uk_sheet(path: Path, number: str, title: str, labels: list[tuple[str, float, float]], shift=(0.0, 0.0)) -> str:
    """An A2 UK title block like DfE's: 'TITLE' label, the title under it at the number's print, the ISO number."""
    doc = pymupdf.open()
    pg = doc.new_page(width=1684, height=1191)
    put = [("TITLE", 1421, 1030, 6.9), (title, 1421, 1050, 12), ("DATE", 1336, 1088, 6.9), ("2026-01-12", 1335, 1106, 12),
           ("PROJECT - ORIGINATOR - FUNCTION - SPATIAL - FORM - DISCIPLINE - NUMBER", 1337, 1130, 6.9),
           (number, 1336, 1150, 12)] + [(t, x, y, 3.9) for t, x, y in labels]
    for t, x, y, fs in put:   # a copy printed with an offset moves everything, title block too
        pg.insert_text((x + shift[0], y + shift[1]), t, fontsize=fs)
    doc.save(path)
    return str(path)


_ROOMS = [("Classroom", 200, 300), ("Laundry", 420, 310), ("Kitchen", 640, 520), ("Head's Office", 300, 700),
          ("Library", 900, 400), ("Medical Room", 1100, 650), ("Plant Room", 1150, 230), ("Corridor", 700, 800)]


def test_iso_number_title_and_level(tmp_path):
    p = _uk_sheet(tmp_path / "a.pdf", "SEN-DFE-XX-01-D-X-0202", "Special School Plan Level 01", [])
    e = xref.sheet_index(p)[0]
    assert (e.number, e.title, e.level, e.level_name) == ("SEN-DFE-XX-01-D-X-0202", "Special School Plan Level 01", "1", "Level 01")
    assert xref.ISO_NUMBER.match("PRJ-ARC-ZZ-00-DR-A-1001") and not xref.ISO_NUMBER.match("W9126G-19-D-0008")


def test_tile_cut_labels_are_joined_not_neighbours():
    L = xref._Line
    a = L("SEN-DFE-XX-00-", 60, (11113, 9497, 11855, 9582), (11113, 9497, 11855, 9582), (1, 0))
    b = L("-00-D-X-0201", 60, (11728, 9495, 12300, 9579), (11728, 9495, 12300, 9579), (1, 0))
    c = L("Group", 30, (100, 100, 160, 130), (100, 100, 160, 130), (1, 0))
    d = L("Room", 30, (150, 100, 200, 130), (150, 100, 200, 130), (1, 0))   # overlapping boxes, no shared seam text
    out = xref._join_tile_cuts([a, b, c, d])
    assert sorted(l.text for l in out) == ["Group", "Room", "SEN-DFE-XX-00-D-X-0201"]


def test_register_recovers_a_scan_transform_through_ocr_typos():
    import cmath

    s, t = 2.78 * cmath.exp(1j * -0.01), complex(35, -12)
    L = xref._Line
    la, lb = [], []
    typos = {"Classroom": "Classro:m", "Laundry": "Laundy", "Kitchen": "Kitchen", "Library": "Librarv"}
    for txt, x, y in _ROOMS + [("Group Room", 150, 900), ("Group Room", 450, 900), ("Store", 820, 760)]:
        la.append(L(txt, 4, (x, y, x + 30, y + 5), (x, y, x + 30, y + 5), (1, 0)))
        z0, z1 = s * complex(x, y) + t, s * complex(x + 30, y + 5) + t
        lb.append(L(typos.get(txt, txt), 11, (z0.real, z0.imag, z1.real, z1.imag), (z0.real, z0.imag, z1.real, z1.imag), (1, 0)))
    tr = xref.register(la, 1684, 1191, lb, 4710, 3353)
    assert tr and tr["inliers"] >= 9
    assert abs(complex(tr["a"], tr["b"]) - s) < 0.01 and abs(complex(tr["tx"], tr["ty"]) - t) < 3


def test_copy_rules():
    E = xref.SheetEntry
    full = E(0, "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", level="0")
    assert xref._copy_of(full, E(0, "SEN-DFE-XX-00…", "Speciai Schoo! Pian Level 00", level="0"))
    assert xref._copy_of(full, E(0, "SEN-DFE-XX-00-D-X-0201", None))
    assert not xref._copy_of(full, E(0, "SEN-DFE-XX-01-D-X-0202", "Special School Plan Level 01", level="1"))
    assert not xref._copy_of(full, E(0, "SEN-DFE-XX-00-D-X-0301", None, level="0"))
    assert xref._copy_of(E(0, None, "Ground Floor Plan", level="0"), E(0, None, "GROUND FLOOR PLAN", level="0"))


def test_copy_and_floor_links_on_a_two_floor_set(tmp_path):
    ground = _ROOMS + [("Stair", 330, 240), ("Lift", 350, 850), ("Multi-Purpose", 830, 600), ("Hall", 845, 604.5)]
    upper = [("Music Room", 200, 300), ("Science", 420, 310), ("Stair", 331, 228), ("(Upper)", 328, 232.5),
             ("Lift", 352, 853), ("Void", 790, 600), ("Stair", 360, 250), ("(Roof)", 360, 254.5)] + _ROOMS[4:7]
    p0 = _uk_sheet(tmp_path / "L00.pdf", "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", ground)
    p1 = _uk_sheet(tmp_path / "L01.pdf", "SEN-DFE-XX-01-D-X-0202", "Special School Plan Level 01", upper)
    p0b = _uk_sheet(tmp_path / "L00-copy.pdf", "SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", ground, shift=(4, -3))
    fs = [p0, p1, p0b]
    refs = xref.page_refs(p1, 0, set_paths=fs)
    lv = {(r.text, r.target_view.split(" · ")[0]) for r in refs if r.kind == "level"}
    assert ("Void", "Multi-Purpose Hall") in lv and ("Stair (Upper)", "Stair") in lv and ("Lift", "Lift") in lv
    assert not any(r.text.startswith("Stair (Roof)") for r in refs if r.kind == "level")   # climbs to the roof only
    assert all(r.target_file == p0 for r in refs if r.kind == "level")                     # one plan per floor
    r0 = xref.page_refs(p0, 0, set_paths=fs)
    cp = [r for r in r0 if r.kind == "copy"]
    assert len(cp) == 1 and cp[0].target_file == p0b and cp[0].transform["inliers"] >= 6
    assert abs(cp[0].transform["tx"] - 4) < 1.5 and abs(cp[0].transform["ty"] + 3) < 1.5
    rel = xref.related_sheets(p0, 0, set_paths=fs)
    assert {(x["kind"], os.path.basename(x["file"])) for x in rel} == {("copy", "L00-copy.pdf"), ("floor", "L01.pdf")}
    assert not [r for r in xref.page_refs(p0, 0) if r.kind in ("copy", "level")]          # alone: nothing to link


@pytest.mark.skipif(not (DFE / "SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf").exists(), reason="no DfE set")
def test_dfe_main_line_set():
    fs = [str(DFE / "SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf"),
          str(DFE / "SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf")]
    e0, e1 = (xref.sheet_index(f)[0] for f in fs)
    assert (e0.number, e0.title, e0.level) == ("SEN-DFE-XX-00-D-X-0201", "Special School Plan Level 00", "0")
    assert (e1.number, e1.level) == ("SEN-DFE-XX-01-D-X-0202", "1")
    voids = [r for r in xref.page_refs(fs[1], 0, set_paths=fs) if r.kind == "level" and r.detail == "void"]
    assert len(voids) == 2 and all(r.target_view.startswith("Multi-Purpose Hall") for r in voids)
    from stoa.solvers.raster.sheet import cached_layer

    if DFE_SCAN.exists() and cached_layer(str(DFE_SCAN)) is not None:   # the scan's text layer is read on upload only
        cp = [r for r in xref.page_refs(fs[0], 0, set_paths=fs + [str(DFE_SCAN)]) if r.kind == "copy"]
        assert len(cp) == 1 and cp[0].transform["inliers"] >= 20 and abs(cp[0].transform["a"] - 2.78) < 0.05


def test_scanned_pdf_reads_the_warm_up_render_layer(tmp_path, monkeypatch):
    """A one-page PDF with no text of its own: the cached text layer of its page render, scaled px -> pdf_pt."""
    doc = pymupdf.open()
    doc.new_page(width=1684, height=1191)
    p = tmp_path / "scan.pdf"
    doc.save(p)
    zoom = 191 / 72
    L = xref._Line

    def px(t, x, y, h=12):
        b = (x * zoom, y * zoom, (x + 8 * len(t)) * zoom, (y + h) * zoom)
        return L(t, h * zoom * 0.8, b, b, (1, 0))

    layer = [px("TITLE", 1421, 1023, 7), px("Special School Plan Level 00", 1421, 1038), px("SEN-DFE-XX-00-", 1336, 1141)]
    import stoa.formats.normalize as N

    monkeypatch.setattr(N, "render_png", lambda pdf, *a, **k: (str(tmp_path / "scan.r191.png"), zoom))
    monkeypatch.setattr(xref, "_image_lines", lambda png: (layer, 1684 * zoom, 1191 * zoom))
    lines, W, H = xref._lines_of(str(p), 0)
    assert (W, H) == (1684, 1191) and abs(lines[2].src[0] - 1336) < 0.01
    e = xref.sheet_index(str(p))[0]
    assert (e.number, e.title, e.level) == ("SEN-DFE-XX-00…", "Special School Plan Level 00", "0")
    monkeypatch.setattr(xref, "_image_lines", lambda png: None)     # not read yet: nothing, and nothing cached
    assert xref.sheet_index(str(p))[0].number is None


def test_flag_accepts_all(monkeypatch):
    for v, on in (("xref", True), ("all", True), ("region, xref", True), ("", False), ("region", False)):
        monkeypatch.setenv("STOA_FEATURES", v)
        assert xref.enabled() is on


def test_reconcile_schedule_rows_against_plan_tags(tmp_path):
    res = xref.reconcile(_set(tmp_path))
    door = next(t for t in res["tables"] if t["kind"] == "door")
    assert (door["sheet"], door["rows"], door["tagged"]) == ("A-601", 3, 1)
    assert sorted(u["key"] for u in door["untagged"]) == ["11", "13"]
    assert door["links"]["12"][0]["sheet"] == "A-101" and door["links"]["12"][0]["page"] == 0
    assert set(door["row_boxes"]) == {"11", "12", "13"}


def test_reconcile_doors_numbered_after_their_room(tmp_path):
    """AFCEE MEDIUMFC: the plan prints room numbers only ('7.04'); the door schedule numbers doors room + leaf ('7.04A',
    '7.04B'), in two side-by-side tables that share the title 'DOOR SCHEDULE'."""
    doc = pymupdf.open()
    for num, title in [("A-103", "SUPPORT - FLOOR PLAN"), ("A-602", "DOOR SCHEDULE")]:
        pg = doc.new_page(width=1224, height=792)
        pg.insert_text((1100, 600), title, fontsize=10)
        pg.insert_text((1100, 760), num, fontsize=24)
        if num == "A-103":
            for x, k in [(300, "7.04"), (400, "7.00"), (500, "511 SF")]:
                pg.insert_text((x, 300), k, fontsize=8)
        else:
            for x0, keys in [(100, ["5.00A", "7.00A", "7.04A"]), (500, ["7.04B", "20.2A", "3.07A"])]:
                pg.insert_text((x0, 180), "DOOR SCHEDULE", fontsize=9)
                for i, k in enumerate(keys):
                    pg.insert_text((x0, 200 + 12 * i), k, fontsize=8)
                    pg.insert_text((x0 + 60, 200 + 12 * i), "3' - 0\"", fontsize=8)
    p = tmp_path / "mfc.pdf"
    doc.save(p)
    rows = xref.schedule_rows(str(p), 1)
    assert [r["key"] for r in rows] == ["5.00A", "7.00A", "7.04A", "7.04B", "20.2A", "3.07A"]
    assert max(r["bbox"][2] for r in rows[:3]) < 500          # a row of the left table stops before the right table
    door = next(t for t in xref.reconcile(str(p))["tables"] if t["kind"] == "door")
    assert door["rows"] == 6 and door["tagged"] == 3
    assert door["links"]["7.04B"][0]["via"] == "room 7.04" and door["links"]["7.04B"][0]["sheet"] == "A-103"
    assert sorted(u["key"] for u in door["untagged"]) == ["20.2A", "3.07A", "5.00A"]
    tag = next(r for r in xref.page_refs(str(p), 0) if r.kind == "tag" and r.text == "7.04")
    assert tag.target_page == 1 and "7.04A, 7.04B" in tag.target_view and tag.subject == "door"
