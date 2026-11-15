"""VP: sheet views (stoa.sheet). Logic tests run anywhere; real-sheet tests skip when the sheet is not on this machine
(the dataset is outside git)."""

import os

import pytest

from stoa.sheet import pick_views, scope_for, views
from stoa.sheet.views import View, _keys, _snap, subject_of, view_kind

NEUTRAL = ".stoa/neutral/x1sl"
MFC = f"{NEUTRAL}/6c15391d66/6c15391d66.pdf"      # AFCEE Fitness Center A-101: 2 modules x (plan, RCP, axon), /Rotate 90
DORM = f"{NEUTRAL}/4c33719c7c/4c33719c7c.pdf"     # AFCEC dorm A-2: untitled main plan + KEY PLAN + legend, /Rotate 90
M101 = f"{NEUTRAL}/acf1f07a2d/acf1f07a2d.pdf"     # USACE M-101A piping plan, 1/4" = 1'-0"
S100 = f"{NEUTRAL}/d1f20c81a3/d1f20c81a3.pdf"     # USACE S-100F foundation and slab plan, 1:50, 'GENSET FOUNDATION' label
A101F = f"{NEUTRAL}/c6ff0aa6e6/c6ff0aa6e6.pdf"    # USACE A-101F filter building floor plan 1:50
EFA = f"{NEUTRAL}/017488d83b/017488d83b.pdf"      # EFA secondary GF, A3 page printed '1:200 @ A1'
DFE = f"{NEUTRAL}/d5b889d203/d5b889d203.pdf"      # DfE special school plan level 00, 1:200 on A2


def need(path):
    if not os.path.exists(path):
        pytest.skip(f"{path} not on this machine")
    return path


def _v(kind, title, bbox=(0, 0, 10, 10)):
    return View(kind=kind, title=title, bbox=bbox, title_bbox=(0, 0, 1, 1), subject=subject_of(title))


# ---- logic -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title,kind", [
    ("MODULE 01- ENTRY VESTIBULE - FLOOR PLAN", "floor_plan"),
    ("MODULE 01 - ENTRY VESTIBULE - RCP", "rcp"),
    ("REFLECTED CEILING PLAN - LEVEL 2", "rcp"),
    ("MODULE 02-ADMINISTRATION- AXON.", "axon"),
    ("FOUNDATION AND SLAB PLAN", "foundation_plan"),
    ("PIPING PLAN", "services_plan"),
    ("KEY PLAN", "key_plan"),
    ("BUILDING SECTION A-A", "section"),
    ("NORTH ELEVATION", "elevation"),
    ("DOOR SCHEDULE", "schedule"),
    ("GENERAL NOTES", "notes"),
    ("SECOND FLOOR PLAN", "floor_plan"),
    ("Special School Plan Level 00", "floor_plan"),
    ("ROOF PLAN", "roof_plan"),
])
def test_view_kind(title, kind):
    assert view_kind(title) == kind


def test_subject_and_keys():
    assert subject_of("MODULE 02-ADMINISTRATION - FLOOR PLAN") == "MODULE 02 ADMINISTRATION"
    assert _keys("How many doors does the administration module (module 02) have?")["module"] == "2"
    assert _keys("SECOND FLOOR PLAN")["floor"] == "2"
    assert _keys("What is the area of the second floor?")["floor"] == "2"
    assert _keys("LEVEL 01 PLAN")["floor"] == "1"


def test_snap_paper_factor():
    assert _snap(841 / 420) == pytest.approx(2.0)     # A1 note on an A3 page
    assert _snap(34 / 17) == pytest.approx(2.0)       # 22x34 note on an 11x17 page
    assert _snap(1.3) == 1.3


def test_pick_named_module_and_never_rcp():
    vs = [_v("floor_plan", "MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"), _v("rcp", "MODULE 01 - ENTRY VESTIBULE - RCP"),
          _v("axon", "MODULE 01-ENTRY VESTIBULE- AXON."), _v("floor_plan", "MODULE 02-ADMINISTRATION - FLOOR PLAN"),
          _v("rcp", "MODULE 02 - ADMINISTRATION - RCP"), _v("axon", "MODULE 02-ADMINISTRATION- AXON.")]
    p = pick_views("How many doors does the administration module (module 02) have?", vs)
    assert [v.title for v in p.views] == ["MODULE 02-ADMINISTRATION - FLOOR PLAN"]
    p = pick_views("How many doors are there in the entry vestibule module?", vs)
    assert [v.title for v in p.views] == ["MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"]
    p = pick_views("How big is the staff workstation area?", vs)
    assert {v.kind for v in p.views} == {"floor_plan"} and len(p.views) == 2
    p = pick_views("How many light fixtures are in the ceiling of module 01?", vs)
    assert [v.title for v in p.views] == ["MODULE 01 - ENTRY VESTIBULE - RCP"]


def test_pick_storeys():
    vs = [_v("floor_plan", "GROUND FLOOR PLAN"), _v("floor_plan", "FIRST FLOOR PLAN"), _v("floor_plan", "SECOND FLOOR PLAN"),
          _v("key_plan", "KEY PLAN"), _v("section", "SECTION A-A")]
    assert [v.title for v in pick_views("How many rooms are on the second floor?", vs).views] == ["SECOND FLOOR PLAN"]
    assert [v.title for v in pick_views("What is the area of the ground floor?", vs).views] == ["GROUND FLOOR PLAN"]
    whole = pick_views("How many toilets does the whole building have?", vs).views
    assert [v.title for v in whole] == ["GROUND FLOOR PLAN", "FIRST FLOOR PLAN", "SECOND FLOOR PLAN"]


# ---- real sheets -------------------------------------------------------------------------------------------------

def test_mfc_six_views():
    vs = [v for v in views(need(MFC)) if v.kind not in ("notes", "schedule")]
    assert [v.kind for v in vs] == ["floor_plan", "rcp", "axon", "floor_plan", "rcp", "axon"]
    assert [v.number for v in vs] == ["1", "2", "3", "4", "5", "6"]
    assert all(v.ratio == 96 and v.bbox for v in vs)
    s = scope_for(MFC, "How many doors does the administration module (module 02) have?")
    assert [v.title for v in s.views] == ["MODULE 02-ADMINISTRATION - FLOOR PLAN"]
    # a module 02 room tag ('Staff Workstations 2.00 702 SF', pdf_pt) is inside, a module 01 one ('Lobby 569 SF') is not
    assert s.contains(1150, 1960) and not s.contains(580, 1935)


def test_mfc_scoped_room_tags():
    from stoa.sheet.labels import scoped_tags

    tags, s = scoped_tags(need(MFC), "What is the total floor area of all rooms in the entry vestibule module (module 01)?")
    assert sum(t.value for t in tags) == 1388 and len(tags) == 8
    tags, _ = scoped_tags(MFC, "What is the combined area of the four private offices in the administration module?")
    offices = [t for t in tags if "Office" in (t.name or "")]
    assert sum(t.value for t in offices) == 362
    tags, _ = scoped_tags(MFC, "How big is the staff workstation area?")
    assert [t.value for t in tags if (t.name or "").startswith("Staff Workstations")] == [702]  # plan only, not the RCP


def test_dorm_key_plan_and_untitled_main_plan():
    vs = views(need(DORM))
    kinds = {v.kind: v for v in vs}
    assert "key_plan" in kinds and kinds["floor_plan"].title == "SECOND FLOOR PLAN"
    s = scope_for(DORM, "How many dorm rooms are drawn on this floor?")
    assert [v.kind for v in s.views] == ["floor_plan"]


def test_single_view_sheets_need_no_scope():
    for path, q in ((M101, "How many fuel pumps (FP) are shown on the piping plan?"),
                    (S100, "How big is the genset foundation in plan?"),
                    (A101F, "How many doors does the filter building have?")):
        need(path)
        assert scope_for(path, q) is None
    s100 = [v for v in views(S100) if v.kind in ("foundation_plan",)]
    assert [v.title for v in s100] == ["FOUNDATION AND SLAB PLAN"] and s100[0].ratio == 50
    m101 = [v for v in views(M101) if v.kind == "services_plan"]
    assert m101[0].ratio == 48 and m101[0].number == "A1"


def test_paper_note_rescale_and_large_print_titles():
    v = [v for v in views(need(EFA)) if v.kind == "floor_plan"][0]
    assert v.ratio == 200 and v.page_ratio == pytest.approx(400)
    d = [v for v in views(need(DFE)) if v.kind == "floor_plan"][0]
    assert d.title == "Special School Plan Level 00" and d.ratio == 200


def test_not_a_pdf_or_missing():
    assert views("/nonexistent/sheet.png") == []
    assert views("/nonexistent/sheet.pdf") == []
    assert scope_for("/nonexistent/sheet.pdf", "How many doors?") is None


def test_part_words_do_not_switch_kind():
    vs = [_v("floor_plan", "FIRST FLOOR PLAN"), _v("section", "BUILDING SECTION A-A"), _v("elevation", "NORTH ELEVATION")]
    assert pick_views("How many rooms are in the north section of the building?", vs).views[0].kind == "floor_plan"
    assert pick_views("What is the finished floor elevation?", vs).views[0].kind == "floor_plan"
    assert pick_views("How many windows are on the north elevation?", vs).views[0].kind == "elevation"
    assert pick_views("What is the ceiling height in section A-A?", vs).views[0].kind == "section"


def test_crop_maps_back_to_pdf_pt():
    import pymupdf

    from stoa.sheet.crops import Crop, crop_for
    from stoa.sheet.labels import room_tags

    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    for rot in (0, 90, 180, 270):
        pg.set_rotation(rot)
        c = Crop("x", None, 72, (0, 0, 0, 0), rot, pg.mediabox.width, pg.mediabox.height)
        ref = pymupdf.Point(300, 150) * pg.derotation_matrix
        assert c.to_pdf_pt(300, 150) == pytest.approx((ref.x, ref.y), abs=0.01)
    crops = crop_for(need(MFC), "How many doors does the administration module (module 02) have?")
    assert len(crops) == 1 and os.path.exists(crops[0].png)
    c = crops[0]
    t = [t for t in room_tags(MFC) if t.text == "702 SF" and t.view_kind == "floor_plan"][0]
    with pymupdf.open(MFC) as d:
        r = pymupdf.Rect(t.area_box) * d[0].rotation_matrix
    px = [(r.x0 - c.clip[0]) * c.dpi / 72, (r.y0 - c.clip[1]) * c.dpi / 72,
          (r.x1 - c.clip[0]) * c.dpi / 72, (r.y1 - c.clip[1]) * c.dpi / 72]
    assert c.box_to_pdf_pt(px) == pytest.approx(t.area_box, abs=0.05)
    assert crop_for(need(M101), "How many fuel pumps are shown?") == []   # single-view sheet: no crop


def test_us_ground_floor_and_upper_floor():
    us = [_v("floor_plan", "FIRST FLOOR PLAN"), _v("floor_plan", "SECOND FLOOR PLAN"), _v("key_plan", "KEY PLAN")]
    p = pick_views("How many rooms are on the ground floor?", us)
    assert [v.title for v in p.views] == ["FIRST FLOOR PLAN"] and "US numbering" in p.reason
    p = pick_views("What is the area of the upper floor?", us)
    assert [v.title for v in p.views] == ["SECOND FLOOR PLAN"]
    uk = [_v("floor_plan", "GROUND FLOOR PLAN"), _v("floor_plan", "FIRST FLOOR PLAN")]
    assert [v.title for v in pick_views("How many rooms are on the ground floor?", uk).views] == ["GROUND FLOOR PLAN"]
    assert [v.title for v in pick_views("How many rooms are on the first floor?", uk).views] == ["FIRST FLOOR PLAN"]


AFCEC_SD = ".stoa/datasets/stoa-like/afcec-sd"


def test_mediumfc_module_pages_and_cover_sheet():
    mfc = need(f"{AFCEC_SD}/MEDIUMFC_Architectural_Sheets_v1.0.pdf")
    vs = [v for v in views(mfc, 3) if v.kind in ("floor_plan", "rcp", "axon")]
    assert [(v.kind, v.number) for v in vs] == [("floor_plan", "1"), ("rcp", "2"), ("axon", "3"),
                                               ("floor_plan", "4"), ("rcp", "5"), ("axon", "6")]
    s = scope_for(mfc, "How many doors does the FAC module have?", 3)
    assert [v.title for v in s.views] == ["MODULE 04 - FAC - FLOOR PLAN"]
    s = scope_for(mfc, "What is the area of module 03?", 3)
    assert [v.title for v in s.views] == ["MODULE 03 - HAWC - FLOOR PLAN"]
    # a cover sheet's drawing index ('A-101 LIFE SAFETY PLAN - FIRST FLOOR', …) is not a set of views
    ftc = need(f"{AFCEC_SD}/FTC_DefinitiveDesignDrawings.pdf")
    assert not [v for v in views(ftc, 1) if v.kind in ("floor_plan", "services_plan", "site_plan")]


def test_sets_storeys_and_whole_building():
    import json

    from stoa.sheet.sets import pick_sheets, sheet_index, storeys

    dorm = need(".stoa/datasets/stoa-like/afcec-dorm/DAF_Dorm_SD_Drawings_April_2026.pdf")
    idx = sheet_index(dorm)
    assert [s.number for s in idx[:4]] == ["A-0", "A-1", "A-2", "A-3"]
    assert [r["storey"] for r in storeys(idx)] == ["1", "2", "3"]
    ch, p = pick_sheets("How many rooms are on the second floor?", idx)
    assert {s.storey for s in ch} == {"2"}
    ch, p = pick_sheets("What is the area of the ground floor?", idx)
    assert {s.storey for s in ch} == {"1"} and "US numbering" in p.reason
    ch, p = pick_sheets("How many doors does the whole building have?", idx)
    assert {"1", "2", "3"} <= {s.storey for s in ch} and not any(s.partial for s in ch)
    mfc = need(f"{AFCEC_SD}/MEDIUMFC_Architectural_Sheets_v1.0.pdf")
    ms = sheet_index(mfc, max_pages=4)
    assert ms[0].kind == "cover" and [s.kind for s in ms[2:4]] == ["floor_plan", "floor_plan"]
    assert ms[2].number == "A-101"


def test_scale_bar_reveals_a_print_at_another_size(tmp_path):
    import pymupdf

    src = pymupdf.open(need(DFE))                    # 1:200 note + bar 0 / 10m / 20m, no paper-size note
    v = [v for v in views(DFE) if v.kind == "floor_plan"][0]
    assert v.bar_ratio == pytest.approx(200, rel=0.05) and v.page_ratio == 200
    half = pymupdf.open()
    r = src[0].rect
    pg = half.new_page(width=r.width / 2, height=r.height / 2)
    pg.show_pdf_page(pg.rect, src, 0)                # the same sheet printed at half size: the note still says 1:200
    out = tmp_path / "dfe_half.pdf"
    half.save(str(out))
    v2 = [v for v in views(str(out)) if v.kind == "floor_plan"][0]
    assert v2.ratio == 200 and v2.page_ratio == pytest.approx(400, rel=0.01)
    assert any("printed at another size" in n for n in v2.notes)


def test_pick_sheets_buildings_and_storeys():
    from stoa.sheet.sets import Sheet, pick_sheets, storeys

    def sh(n, title, storey, building=None, kind="floor_plan", partial=False):
        return Sheet(file="set.pdf", page=n, number=f"A-{n}", title=title, kind=kind, storey=storey,
                     storey_name=None, building=building, partial=partial)
    idx = [sh(1, "BUILDING A - GROUND FLOOR PLAN", "0", "A"), sh(2, "BUILDING A - FIRST FLOOR PLAN", "1", "A"),
           sh(3, "BUILDING B - GROUND FLOOR PLAN", "0", "B"), sh(4, "BUILDING B - FIRST FLOOR PLAN", "1", "B"),
           sh(5, "BUILDING B - ENLARGED TOILET PLAN", "1", "B", partial=True),
           sh(6, "BUILDING B - FIRST FLOOR REFLECTED CEILING PLAN", "1", "B", kind="rcp")]
    ch, _ = pick_sheets("How many rooms are on the first floor of building B?", idx)
    assert [s.number for s in ch] == ["A-4"]
    ch, _ = pick_sheets("How many doors does building A have?", idx)
    assert [s.number for s in ch] == ["A-1", "A-2"]
    ch, _ = pick_sheets("How many light fixtures are in the ceiling of building B's first floor?", idx)
    assert [s.number for s in ch] == ["A-6"]
    ch, _ = pick_sheets("How many toilets are there in the whole project?", idx)
    assert [s.number for s in ch] == ["A-1", "A-2", "A-3", "A-4"]          # partial plan dropped, RCP not a plan
    assert [(r["building"], r["storey"]) for r in storeys(idx)] == [("A", "0"), ("A", "1"), ("B", "0"), ("B", "1")]


def test_ocr_cut_module_titles():
    vs = [_v("floor_plan", "E 01- ENTRY VESTIBULE - FLOOR PLAN"), _v("rcp", "MODULE 01 - ENTRY VESTIBULE - RCP"),
          _v("floor_plan", "E 02-ADMINISTRATION - FLOOR PLAN"), _v("rcp", "MODULE 02 - ADMINISTRATION - RCP")]
    assert [v.title for v in pick_views("How many doors does module 01 have?", vs).views] == \
        ["E 01- ENTRY VESTIBULE - FLOOR PLAN"]
    assert [v.title for v in pick_views("How many doors does module 2 have?", vs).views] == \
        ["E 02-ADMINISTRATION - FLOOR PLAN"]


# ---- images (text layer read in tiles) ---------------------------------------------------------------------------

def test_tile_cut_titles_are_joined():
    from stoa.sheet.views import _join_tile_cuts, _Line, _repair_cut_words

    def L(t, x0, x1, y=3375, h=48):
        b = (x0, y, x1, y + h)
        return _Line(t, b, b, h)

    out = _join_tile_cuts([L("MODULE 01- ENTRY VESTIE", 1479, 2147), L("VESTIBULE - FLOOR PLAN", 2013, 2636),
                           L("MODULE 01 -", 3841, 4124), L("E 01 - ENTRY VESTIBULE - RCP", 4026, 4798),
                           L("MODULE 01-ENTRY VE", 6641, 7181), L("TRY VESTIBULE- AXON.", 7045, 7601),
                           L("Lobby", 9000, 9100), L("569 SF", 9000, 9100, y=3450)])
    texts = sorted(l.text for l in out)
    assert texts == sorted(["MODULE 01- ENTRY VESTIBULE - FLOOR PLAN", "MODULE 01 - ENTRY VESTIBULE - RCP",
                            "MODULE 01-ENTRY VESTIBULE- AXON.", "Lobby", "569 SF"])
    # neighbours that only touch (no repeated text) stay apart
    assert len(_join_tile_cuts([L("OFFICE", 0, 200), L("STORAGE", 190, 400)])) == 2
    assert _repair_cut_words("OND FLOOR PLAN") == "SECOND FLOOR PLAN"
    assert _repair_cut_words("IRST FLOOR") == "FIRST FLOOR"
    assert _repair_cut_words("ROUND FLOOR PLAN") == "GROUND FLOOR PLAN"
    assert _repair_cut_words("SECOND FLOOR PLAN") == "SECOND FLOOR PLAN"
    assert _repair_cut_words("ESIGN CONCEPT FLOOR PLAN") == "ESIGN CONCEPT FLOOR PLAN"


def test_image_scale_is_px_per_m_never_a_page_ratio():
    from stoa.sheet.views import _image_scales, _Line

    def L(t, x0, y, x1=None, h=40):
        b = (x0, y, x1 or x0 + 20 * len(t), y + h)
        return _Line(t, b, b, h)

    v = _v("floor_plan", "FLOOR PLAN", (0, 0, 9000, 6000))
    v.ratio = 96.0
    lines = [L("SCALE(S) AS NOTED ON THIS SHEET ARE BASED ON A FULL 22X34 SHEET", 7000, 100)]
    _image_scales([v], lines, 10200.0, 6600.0)
    assert abs(v.px_per_m - 300 / 0.0254 / 96) < 0.01 and v.page_ratio is None   # 10200 px / 34 in = 300 dpi
    w = _v("floor_plan", "FLOOR PLAN", (0, 0, 9000, 6000))
    w.ratio = 96.0
    _image_scales([w], [], 10200.0, 6600.0)
    assert w.px_per_m is None and "unknown" in w.notes[-1]


@pytest.mark.parametrize("png,pdf,titles", [
    ("e9adc17ff3/e9adc17ff3.png", MFC, 6),     # tile-cut titles: 2 RCPs + untitled before; 6 views as on the PDF
    ("934e2570a3/934e2570a3.png", DORM, 2),    # key-plan legend 'FIRST FLOOR' / 'THIRD FLOOR' were views; title block
])
def test_png_views_match_the_pdf(png, pdf, titles):
    from stoa.solvers.raster import textlayer as T

    p = need(f"{NEUTRAL}/{png}")
    need(pdf)
    if not T._disk_key(p, 1100, "local").exists():
        pytest.skip("no cached text layer for this image")
    iv = [v for v in views(p, layer=T.text_layer(p)) if v.kind != "notes"]
    pv = [v for v in views(pdf) if v.kind != "notes"]
    assert len(iv) == len(pv) == titles
    assert sorted((v.kind, v.title) for v in iv) == sorted((v.kind, v.title) for v in pv)
    assert all(v.page_ratio is None for v in iv)
    if png.startswith("e9ad"):
        assert all(abs(v.px_per_m - 123.03) < 0.1 for v in iv)   # 22x34 sheet at 300 dpi, 1:96; the bar agrees


def test_ctr_number_area_tags_and_unnamed_totals():
    """S0 d25: CTR A101 tags are 'STUDIO A -' / 'ADA TYPE B' / 'U106 - 468 SF' (number and area on one line); only the
    floor-area table ('1ST FLOOR  21996 SF') was read, 5 unnamed totals -> area_tags abstained on 'largest studio'."""
    from stoa.data import abspath
    from stoa.sheet.labels import as_area_tags, room_tags

    p = need(str(abspath("data/opening_design/CTR/Most Current Set/Full size sheets/A101 - FLOOR PLANS - 1ST FLOOR.pdf")))
    tags = room_tags(p)
    assert len(tags) == 45 and sum(1 for t in tags if t.name) == 39
    by_no = {t.number: t for t in tags}
    assert by_no["U106"].value == 468 and by_no["U106"].name == "STUDIO A - ADA TYPE B"
    assert by_no["103.N"].name == "CORRIDOR - NORTH" and by_no["103.N"].value == 393
    got = as_area_tags(p, "Which is the largest studio apartment and how big is it?")
    assert got and all(d["name"] or d["number"] for d in got if d["value"] < 20000)
    vs = {v.kind for v in views(p)}
    assert "key_plan" in vs and "drawing" in vs   # the key plan no longer swallows the main plan


def test_view_choices_for_interactive_clarification():
    """Captain 18:50 / W5 19:10: an ambiguous view -> best guess + clickable options; a pick re-asks '(view: TITLE)'."""
    from stoa.contract import Answer
    from stoa.sheet import attach_view_choices, view_choices

    p = need(MFC)
    c = view_choices(p, "How many doors are on this plan?")
    assert c and c["ask"] == "Which plan do you mean?" and len(c["options"]) == 2
    assert {o["label"] for o in c["options"]} == {"MODULE 01- ENTRY VESTIBULE - FLOOR PLAN",
                                                    "MODULE 02-ADMINISTRATION - FLOOR PLAN"}
    s = scope_for(p, c["options"][1]["question"])
    assert [v.title for v in s.views] == [c["options"][1]["label"]]
    assert view_choices(p, "How many doors does module 02 have?") is None          # named
    assert view_choices(p, "How many doors are there in total on the sheet?") is None   # 'total' = all views
    assert view_choices(p, "How big is the gym?") is None                              # a room finds its own view
    assert view_choices(p, "How many light fixtures are in the ceiling?")["ask"] == "Which ceiling plan do you mean?"
    assert view_choices(need(DORM), "How many dorm rooms are on this floor?") is None   # one plan + key plan
    a = attach_view_choices(Answer(value=13, unit="count", confidence=0.6, trace=[]), p, "How many doors are on this plan?")
    assert len(a.evidence) == 2 and a.evidence[0].bbox_space == "pdf_pt" and a.trace[-1].startswith("clarify: Which plan")


def test_sheet_choices_for_sets():
    from stoa.sheet.sets import pick_sheets, sheet_choices, sheet_index

    idx = sheet_index(need(".stoa/datasets/stoa-like/afcec-dorm/"
                           "DAF_Dorm_SD_Drawings_April_2026.pdf"))
    # storey 2 = one whole plan (A-5 is the partial 'SECOND FLOOR COMMON AREA PLAN', its title-block title): no question
    assert sheet_choices("How many rooms are on the second floor?", idx) is None
    ch, p = pick_sheets("How many rooms are on the second floor?", idx)
    assert [s.label for s in ch] == ["A-2 SECOND FLOOR PLAN T-SHAPE"]
    # 'ground floor' on a US set is read as FIRST FLOOR: say so and offer the other storeys
    c = sheet_choices("How many rooms are on the ground floor?", idx)
    assert c and c["options"][0]["label"].startswith("A-1 FIRST FLOOR PLAN") and len(c["options"]) >= 3
    ch, p = pick_sheets(c["options"][1]["question"], idx)
    assert len(ch) == 1 and ch[0].label == c["options"][1]["label"].split(" (")[0]
    ch, p = pick_sheets("How many rooms are on the fifth floor?", idx)
    assert ch == [] and "storeys: 1, 2, 3" in p.reason
    assert len(sheet_choices("How many rooms are on the fifth floor?", idx)["options"]) >= 3
    assert sheet_choices("How many doors does the whole building have?", idx) is None   # duplicates noted, no re-pick
    assert sheet_choices("How many rooms are on the third floor?", idx) is None


def test_join_tile_cut_items_on_text_layer_items():
    from dataclasses import dataclass

    from stoa.sheet.views import join_tile_cut_items

    @dataclass
    class It:
        text: str
        box: list
        tile: int = 0

    a = It("ING DORMITORY =", [7779.0, 1729.0, 8200.0, 1800.0], 17)
    b = It("WEST WING D", [7700.0, 1729.0, 7900.0, 1800.0], 16)   # left tile's half
    c = It(",095 SF", [9013.0, 1729.0, 9277.0, 1812.0], 18)
    d = It("6,095", [8900.0, 1729.0, 9100.0, 1812.0], 17)   # real cuts repeat >= 3 chars (",095")
    out = join_tile_cut_items([a, b, c, d, It("", [0, 0, 1, 1])])
    texts = sorted(i.text for i in out)
    assert "WEST WING DORMITORY =" in texts and "6,095 SF" in texts and "" in texts and len(out) == 3
    assert a.text == "ING DORMITORY =" and b.text == "WEST WING D"   # inputs untouched


def test_set_titles_from_title_block_and_cover():
    """A set's sheet title = its title block's own line (the one not repeated on the other sheets), two-line and
    edge-running titles joined; the drawing-index page is the cover, whatever its index rows look like."""
    from stoa.sheet.sets import sheet_index

    dorm = need(".stoa/datasets/stoa-like/afcec-dorm/DAF_Dorm_SD_Drawings_April_2026.pdf")
    idx = sheet_index(dorm)
    assert idx[0].kind == "cover" and idx[0].storey is None
    # = the cover's own index of drawings, A-1 .. A-8
    assert [s.title for s in idx[1:]] == [
        "FIRST FLOOR PLAN T-SHAPE", "SECOND FLOOR PLAN T-SHAPE", "THIRD FLOOR PLAN T-SHAPE", "FIRST FLOOR COMMON AREA PLAN",
        "SECOND FLOOR COMMON AREA PLAN", "THIRD FLOOR COMMON AREA PLAN", "DORMITORY MODULE", "ACCESSIBLE (ABA) DORM MODULE"]
    assert [(s.storey, s.partial) for s in idx[4:7]] == [("1", True), ("2", True), ("3", True)]
    aw = need(".stoa/datasets/stoa-like/originals/AW_078-24-33.pdf")
    by = {s.number: s for s in sheet_index(aw, max_pages=120)}
    assert by["G-001"].kind == "cover" and by["M-201"].title == "FLOW DIAGRAM"
    assert by["EP501"].title == "TELECOM. & ATG BACKBOARDS" and by["S-001"].title == "STRUCTURAL GENERAL NOTES"
    assert by["EI602"].title == "CONTROL SYSTEM I/O SHEET 1" and not [n for n, s in by.items() if not s.title]
    sfo = need(f"{AFCEC_SD}/SFO_PDF_Version_of_all_Sheets_v1.0.pdf")
    assert sheet_index(sfo, max_pages=2)[1].title == "Police Service & Installation Security/ S5"


def test_project_for_an_uploaded_set():
    """T3's call: every sheet with role / level / title, the storey table, the sheets a building total sums."""
    import json

    from stoa.sheet.sets import project

    dorm = need(".stoa/datasets/stoa-like/afcec-dorm/DAF_Dorm_SD_Drawings_April_2026.pdf")
    pr = project([dorm])
    json.dumps(pr)                                                     # JSON-ready
    rows = pr["sheets"]
    assert [r["role"] for r in rows[:2]] == ["cover / index", "floor plan"]
    assert [r["level"] for r in rows[1:4]] == ["FIRST FLOOR", "SECOND FLOOR", "THIRD FLOOR"]
    # totals: the three storey plans; never the cover, the partial common-area plans or the 1:96 module enlargements
    assert [r["number"] for r in rows if r["for_totals"]] == ["A-1", "A-2", "A-3"]
    assert "enlargements" in pr["totals"]["why"] and not pr["notes"]
    assert [r["storey"] for r in pr["storeys"]] == ["1", "2", "3"] and pr["storeys"][0]["sheets"][0] == rows[1]["id"]
    assert rows[1]["scale"] == "1:480" and rows[1]["views"] and rows[1]["views"][0]["bbox_space"] == "pdf_pt"
    # an image nobody has read yet: listed, role unknown, said so (no OCR from here)
    img = ".stoa/datasets/stoa-like/originals/AW_078-24-33.p69.r136.png"
    if os.path.exists(img):
        import shutil
        import tempfile

        d = tempfile.mkdtemp()
        fresh = shutil.copy(img, os.path.join(d, "never_read_vp_test.png"))
        pi = project([fresh])
        assert pi["sheets"][0]["text"] == "none" and "not read yet" in pi["notes"][0]


def test_north_arrow_on_real_sheets():
    """Captain 20:47 (facade questions): north on the displayed sheet, or 'not found' -> top = north, assumed."""
    from stoa.sheet.north import north_arrow

    H = ".stoa/human/drawings"
    r = north_arrow(need(f"{H}/d5b889d203-f4c030a4.pdf"))          # Stoa's DfE sheet 1: no arrow drawn
    assert not r["found"] and r["assumed"] and r["sides"]["west"] == "left" and "top of the sheet" in r["note"]
    r = north_arrow(need(f"{H}/d1f20c81a3-17b2e2ee.pdf"))          # S-100F: a boxed 'N' label, no arrow
    assert not r["found"]
    dorm = need(".stoa/datasets/stoa-like/afcec-dorm/DAF_Dorm_SD_Drawings_April_2026.pdf")
    r = north_arrow(dorm, 1)                                       # 'PLAN NORTH' over a needle, /Rotate 90 page
    assert r["found"] and r["angle_deg"] == 0 and r["sides"] == {"north": "top", "east": "right", "south": "bottom",
                                                                 "west": "left"}
    r = north_arrow(need(f"{NEUTRAL}/894f61cbda/894f61cbda.pdf"))   # EFA: 'N' over a circle with a needle
    assert r["found"] and r["angle_deg"] == 0
    r = north_arrow(need(f"{H}/56c726cab3-bea8a191.pdf"), 1)       # a compass turned ~17 deg clockwise
    assert r["found"] and 10 <= r["angle_deg"] <= 25 and r["sides"]["west"] == "left"
    r = north_arrow(need(f"{NEUTRAL}/2149733086/2149733086.pdf"), 3)  # '(N) NIGHT …' in a symbol legend: not an arrow
    assert not r["found"]


def test_north_arrow_on_images_matches_pdf_twins():
    """Stoa's PNG copies: the label from the cached text layer, the symbol from dark components (text ink blanked)."""
    from stoa.sheet.north import north_arrow

    r = north_arrow(need(f"{NEUTRAL}/934e2570a3/934e2570a3.png"))  # AFCEC dorm A-2 PNG: PLAN NORTH over a needle
    if "not read yet" in r["note"]:
        pytest.skip("no cached text layer for the image")
    assert r["found"] and r["angle_deg"] == 0 and r["bbox_space"] == "image_px"
    r = north_arrow(need(f"{NEUTRAL}/d5b889d203/d5b889d203.r150.png"))   # DfE sheet 1 render: no arrow (= its PDF)
    assert not r["found"] and r["assumed"]


def test_scale_bars_pair_with_their_own_view():
    """F3 20:49: three 1" = 1'-0" sections with their own bars + a 3/8" plan with its bar: the last bar used to be
    written to every view sharing the nearest note's ratio (sections got 1:33.03); now each view keeps its own bar."""
    aw = need(".stoa/datasets/stoa-like/originals/AW_078-24-28_march_2020.pdf")
    vs = [v for v in views(aw, 20) if v.ratio]
    plan = [v for v in vs if v.kind == "floor_plan"][0]
    assert plan.ratio == 32 and plan.bar_ratio == pytest.approx(33.03, rel=0.01)
    assert any("agrees" in n for n in plan.notes)
    assert all(v.bar_ratio is None or v.bar_ratio < 14 for v in vs if v.ratio == 12)


def test_iso_sheet_numbers_site_plan_and_unread_scan(tmp_path):
    """DfE sheets: the ISO 19650 drawing number, not the paper size under SIZE ('A2') or the revision ('C01'); a
    'Landscape Masterplan' is a site plan (out of the totals); a text-less PDF page nobody read yet stays out too."""
    import pymupdf
    from stoa.sheet.sets import classify, project

    d = ".stoa/datasets/stoa-like/dfe/"
    l00 = need(d + "SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf")
    land = need(d + "Special_school_landscape_masterplan.pdf")
    s = classify(l00, 0)
    assert s.number == "SEN-DFE-XX-00-D-X-0201" and s.storey == "0"
    blank = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    doc.new_page(width=1684, height=1191)
    doc.save(str(blank))
    r = project([l00, land, str(blank)])
    by = {row["file"]: row for row in r["sheets"]}
    assert by[land]["kind"] == "site_plan" and not by[land]["for_totals"]
    assert by[l00]["for_totals"] and not by[str(blank)]["for_totals"]
    assert any("no text read yet" in n for n in r["notes"])


def test_footprint_gross_internal_area_matches_printed_gifa():
    """stoa.sheet.footprint on EFA sheets that print their GIFA: ground floor exact (1248 m²), first floor after the
    hall void drawn with an X (932 m²), an untitled single-drawing sheet at its '1:200@A3' note (1268 m²); the outline
    is evidence in pdf_pt."""
    from stoa.sheet.footprint import footprint

    d = ".stoa/datasets/stoa-like/"
    gf = footprint(need(d + "dfe-baseline-2012/15723_2-01-001_primary_type_2_420_26.pdf"))
    assert abs(gf["gia_m2"] / 1248 - 1) < 0.02 and gf["gea_m2"] > gf["gia_m2"] and gf["ratio"] == 200
    assert len(gf["polygon"]) >= 4 and all(0 <= x <= 1191 and 0 <= y <= 842 for x, y in gf["polygon"])
    ff = footprint(need(d + "dfe-baseline-2012/15723_2_01_002_primary_type_2_420__26_001.pdf"))
    assert ff["voids"] and abs(ff["gia_m2"] / 932 - 1) < 0.02
    one = footprint(need(d + "dfe-baseline-2014/baseline-design-240-place-primary-school__1fe_240_place_jmi_primary_"
                             "school.pdf"))
    assert one["view"] == "(the sheet's only drawing)" and abs(one["gia_m2"] / 1268 - 1) < 0.02


def test_footprint_voids_by_label_and_roofs_below():
    """DfE SEN level 01: both 'Void' blocks (one with a faint X) are left out; the lower roofs drawn around the upper
    floor are not part of it; level 02 of an EFA primary (a 10 m² plant room over the roofs) stays small."""
    from stoa.sheet.footprint import footprint

    d = ".stoa/datasets/stoa-like/"
    sen = footprint(need(d + "dfe/SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf"))
    assert len(sen["voids"]) == 2 and 850 < sen["gia_m2"] < 1000
    sf = footprint(need(d + "dfe-baseline-2012/15723_2_01_003_primary_type_2_420__26.pdf"))
    assert sf["gia_m2"] < 40


def test_footprint_c6_walls_voids_scale_bars_roofs():
    """VP c6 on EFA / DfE sheets with a printed GIFA: secondary 2F (wall ink starting inside the outline; corridor voids
    cut by dashed lines) 1274; secondary 1F (narrow X strip, small X boxes beside the hall voids) 1172 within 5 %; 3FE
    630 GF (graphic scale bar next to the kitchen) 1645; DfE secondary Level 02 = its own floor, the two roofs beside
    it left out (Level 01's main block is 1,975 m² GEA)."""
    from stoa.sheet.footprint import footprint

    d = ".stoa/datasets/stoa-like/"
    s2 = footprint(need(d + "dfe-baseline-2014/baseline-designs-600-place-secondary-sch__secondary_type_3_600_second_"
                            "floor_001.pdf"))
    assert s2["wall_m"] > 0.3 and len(s2["voids"]) == 4 and abs(s2["gia_m2"] / 1274 - 1) < 0.02
    s1 = footprint(need(d + "dfe-baseline-2014/baseline-designs-600-place-secondary-sch__secondary_type_3_600_first_"
                            "floor_001.pdf"))
    assert abs(s1["gia_m2"] / 1172 - 1) < 0.05
    gf = footprint(need(d + "dfe-baseline-2014/baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_"
                            "school_gf_002.pdf"))
    assert abs(gf["gia_m2"] / 1645 - 1) < 0.02
    l2 = footprint(need(d + "dfe/SEC-DFE-XX-02-D-X-0203-Secondary_School_GA_Plan_Level_02-A-C01.pdf"))
    assert 1800 < l2["gea_m2"] < 2200 and any("no room name" in a for a in l2["assumptions"])


def test_footprint_answer_hook():
    """The contract hook: only whole-floor / gross area wordings; GIA by default, GEA when asked, ft² when asked;
    evidence = the outline polygon (pdf_pt, not exact)."""
    from stoa.sheet.footprint import footprint_answer, is_gross_area_question

    assert is_gross_area_question("What is the total floor area on this plan?")
    assert is_gross_area_question("What is the GIFA of level 01?")
    assert is_gross_area_question("How big is the ground floor?")
    assert not is_gross_area_question("What is the floor area of the kitchen?")
    assert not is_gross_area_question("What is the total area of all rooms on this plan?")
    gf = need(".stoa/datasets/stoa-like/dfe-baseline-2012/"
              "15723_2-01-001_primary_type_2_420_26.pdf")
    assert footprint_answer(gf, "How many doors are there?") is None
    a = footprint_answer(gf, "What is the total floor area of this plan?")
    # the sheet prints its GIFA: the printed row is the answer (exact evidence), the measured outline its cross-check
    assert a.unit == "m2" and a.value == 1248 and a.evidence[0].exact is True
    assert a.evidence[1].polygon and a.evidence[1].exact is False
    e = footprint_answer(gf, "What is the gross external area in square feet?")
    assert e.unit == "ft2" and e.value > a.value * 10.7639


def test_gross_area_question_wordings_stay_narrow():
    from stoa.sheet.footprint import is_gross_area_question as g

    assert not g("What is the total floor area of English 1 to English 5?")
    assert not g("Total floor area of the maths classrooms (Maths 1-5)?")
    assert not g("What's the footprint of the generator set foundation?")
    assert g("What is the total floor area of this floor?") and g("What is the building footprint?")
    assert g("What is the total floor area of level 01?")
    for q in ("What is the total area of this plan?", "How many square meters is this floor?", "What is the total square "
              "footage?", "What's the area of the whole floor?", "What is the area of level 01?", "How many m2 is the "
              "building?", "Wie groß ist die Bruttogrundfläche?", "Mikä on kerrosala?"):
        assert g(q), q
    for q in ("What is the area of the ground floor kitchen?", "What is the square footage of the master bedroom?",
              "What is the total area of the kitchen?"):
        assert not g(q), q


def test_footprint_answer_on_a_picture_uses_its_traced_copy():
    """A scan / export PNG: read on P1's cached traced vector copy (never traces itself), evidence in image px,
    confidence capped; Stoa's DfE sheet 1 scan within 2 % of its vector PDF's reading."""
    from stoa.sheet.footprint import footprint_answer

    png = need(".stoa/neutral/x1scan2/b22eef0132/b22eef0132.png")
    pdf = need(".stoa/datasets/stoa-like/dfe/"
               "SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf")
    a = footprint_answer(png, "What is the total floor area on this plan?")
    if a is None:
        pytest.skip("no cached traced copy of the scan on this machine")
    v = footprint_answer(pdf, "What is the total floor area on this plan?")
    assert a.evidence[0].bbox_space == "image_px" and a.confidence <= 0.5
    assert abs(a.value / v.value - 1) < 0.02


def test_footprint_printed_totals_win_and_are_cross_checked():
    """Printed gross rows (EFA 'Overall GIFA' blocks, AFCEC 'TOTAL SECOND FLOOR = 26,562 SF'): the row of the floor
    the question means is the answer; the drawn outline (same floor) or the sheet's own part rows cross-check it;
    targets, room tags and option notes ('FF 55m2 Classroom') are never rows."""
    import glob

    from stoa.sheet.footprint import footprint_answer, printed_gross

    d = ".stoa/datasets/stoa-like/"
    gf = need(d + "dfe-baseline-2012/15723_2-01-001_primary_type_2_420_26.pdf")
    a = footprint_answer(gf, "What is the total floor area of this plan?")
    assert a.value == 1248 and a.confidence >= 0.8 and any("agrees" in x for x in a.assumptions)
    f = footprint_answer(gf, "What is the total floor area of the first floor?")
    assert f.value == 932 and len(f.evidence) == 1          # another floor: this sheet's outline is no evidence
    w = footprint_answer(gf, "What is the GIFA of the whole school?")
    assert w.value == 2190 and any("add up" in x for x in w.assumptions)
    e = footprint_answer(gf, "What is the building footprint?")
    assert e.value > 1300 and e.evidence[0].exact is False   # GEA asked, the sheet prints GIFA: measured
    gf3 = glob.glob(d + "dfe-baseline-2014/*630*gf_002.pdf")
    if gf3:
        assert all(r["value"] != 55 for r in printed_gross(gf3[0]))
        assert footprint_answer(gf3[0], "What is the total floor area on this plan?").value == 1645
    t = glob.glob(d + "dfe-baseline-2014/baseline-design-210*.pdf")
    if t:
        m = footprint_answer(t[0], "What is the total floor area on this plan?")
        assert m.value != 1315 and any("Target" in x for x in m.assumptions)
    dorm = d + "afcec-dorm/DAF_Dorm_SD_Drawings_April_2026.pdf"
    if os.path.exists(dorm):
        s2 = footprint_answer(dorm, "What is the total floor area of this floor?", 2)
        assert (s2.value, s2.unit) == (26562, "ft2") and s2.confidence >= 0.75
        assert abs(footprint_answer(dorm, "Total floor area of this floor in square metres?", 2).value - 2467.7) < 1
    sen = d + "dfe/SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf"
    if os.path.exists(sen):
        assert printed_gross(sen) == []                      # room tags 'A: 63 m2' are not floor totals
        one = footprint_answer(sen, "What is the total floor area of the school?")
        assert one.confidence <= 0.35 and any("other floors" in m for m in one.missing)   # one floor of two



def test_key_plan_zones_afcec_t_shape():
    """VP c6 (captain 23:04, Ziying: 'north wing' is the long wing at the top of the T): AFCEC A-2's KEY PLAN colour
    blocks -> north / west / east wings + the pink core, named from the core with plan north up, each mapped onto the
    main plan (PDF with /Rotate 90 and the Stoa PNG)."""
    from stoa.sheet.keyplan import _to_disp, key_plan_zones, zone_for

    pdf = need(".stoa/neutral/x1sl/4c33719c7c/4c33719c7c.pdf")
    kp = key_plan_zones(pdf)
    by = {z["name"]: z for z in kp["zones"]}
    assert set(by) == {"north wing", "west wing", "east wing", "core"} and kp["confidence"] >= 0.7
    box = {k: _to_disp(pdf, 0, z["plan_box"]) for k, z in by.items()}
    cy = {k: (b[1] + b[3]) / 2 for k, b in box.items()}
    cx = {k: (b[0] + b[2]) / 2 for k, b in box.items()}
    assert cy["north wing"] < cy["core"] and cx["west wing"] < cx["core"] < cx["east wing"]
    # the north wing's box sits on the long wing at the top of the T (displayed pt: x ~ 433..523, y ~ 152..400)
    assert 400 < box["north wing"][0] < 470 and box["north wing"][3] < 450 and box["west wing"][1] > 500
    z = zone_for(pdf, "How many doors are in the north wing?")
    assert z and z["name"] == "north wing" and "KEY PLAN" in z["why"]
    assert zone_for(pdf, "How many doors are there?") is None
    png = ".stoa/artifacts/stoa-examples/afcec-dormitory-A-2-second-floor.png"
    zp = zone_for(png, "How many doors are in the north wing?")
    if zp is not None:   # needs the PNG's text layer cached
        x0, y0, x1, y1 = zp["plan_box"]
        assert 3300 < x0 < 3900 and y1 < 3600 and x1 - x0 < 1200


def test_scope_for_wing_from_key_plan():
    """scope_for: 'the north wing' on a sheet with a KEY PLAN = the wing's box on the main plan (one synthetic plan
    view, the plan's scale kept); facade / side wordings and unnamed questions are not scoped to a wing."""
    from stoa.sheet import scope_for

    pdf = need(".stoa/neutral/x1sl/4c33719c7c/4c33719c7c.pdf")
    s = scope_for(pdf, "How many doors are in the north wing?")
    assert s is not None and s.views[0].title == "NORTH WING (key plan)" and "KEY PLAN" in s.reason
    w = scope_for(pdf, "How many dorm rooms does the west wing have?")
    assert w.views[0].title.startswith("WEST WING")
    for q in ("How many windows are on the north side?", "How many doors on the north facade?",
              "How many doors are there?"):
        r = scope_for(pdf, q)
        assert r is None or "key plan" not in r.views[0].title.lower()


def test_storey_words_finnish_german_swedish():
    """T3 23:09 (Stoa is Finnish): level words read with stoa.features.project.level_from_name's keys (Finnish / Swedish
    count the entrance floor as 1, German 'Obergeschoss' from 0)."""
    from stoa.sheet.sets import _storey_of

    want = {"2. krs": "1", "POHJA 3.KRS": "2", "1. KRS POHJA": "0", "KERROS 2": "1", "KELLARIKERROS": "-1",
            "ULLAKKO": "attic", "VESIKATTO": "roof", "Obergeschoss 2": "2", "Grundriss Untergeschoss": "-1",
            "plan 2. våningen": "1", "källare": "-1", "POHJAKERROS": "0", "1. KERROS": "0", "SECOND FLOOR PLAN": "2"}
    for t, k in want.items():
        assert _storey_of(t)[0] == k, t
    assert _storey_of("2 VAN SPACES")[0] is None


def test_key_plan_zones_on_a_bw_scan():
    """X1's 300-dpi B/W scan of AFCEC A-2: the key plan's blocks are grey tones (wings ~209, core ~197 on paper 231):
    north / west / east wing + core, confidence <= 0.55; needs the scan's cached text layer (skips otherwise)."""
    from stoa.sheet.keyplan import key_plan_zones

    scan = need(".stoa/neutral/x1scan2/5b52dfb0eb/5b52dfb0eb.png")
    kp = key_plan_zones(scan)
    if not kp["found"] and "no text layer" in kp["note"]:
        pytest.skip("scan text layer not cached")
    by = {z["name"]: z for z in kp["zones"]}
    assert set(by) == {"north wing", "west wing", "east wing", "core"} and kp["confidence"] <= 0.55
    assert by["north wing"]["plan_box"][3] < by["core"]["plan_box"][1] + 200
    assert by["west wing"]["plan_box"][2] <= by["east wing"]["plan_box"][0]


# ---- c7: the scale checked on a second length (expert rule xr-11) ------------------------------------------------------

def _resized(src: str, f: float, out: str) -> str:
    """The same sheet printed at another paper size (f = page size factor): the printed 1:N no longer holds."""
    import pymupdf

    with pymupdf.open(src) as s:
        d = pymupdf.open()
        r = s[0].rect
        pg = d.new_page(width=r.width * f, height=r.height * f)
        pg.show_pdf_page(pg.rect, s, 0)
        d.save(out)
    return out


def _dim_sheet(path: str, n: float, page_w: float = 842.0) -> str:
    """A drawn sheet at 1:n with 6 dimension strings ('3600', '4800', ...) on their own lines with end ticks."""
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=page_w, height=595)
    k = 72 / 25.4 / n  # pt per mm at 1:n
    y = 80
    for mm in (3600, 4800, 2400, 6000, 3000, 5400):
        x0, x1 = 60, 60 + mm * k
        pg.draw_line((x0, y), (x1, y), width=0.3)
        for x in (x0, x1):
            pg.draw_line((x, y - 4), (x, y + 4), width=0.3)
        pg.insert_text(((x0 + x1) / 2 - 8, y - 2), str(mm), fontsize=6)
        y += 40
    pg.draw_rect(pymupdf.Rect(50, 50, 600, 330), width=1.0)
    pg.insert_text((60, 400), "GROUND FLOOR PLAN", fontsize=12)
    pg.insert_text((60, 416), "SCALE 1:100", fontsize=8)
    d.save(path)
    return path


def test_dimension_scale_checks_printed_scale(tmp_path):
    """VP c7: 6 dimension strings drawn at 1:100 confirm a printed 1:100; the same strings drawn at 1:200 (the sheet
    printed at half size, note unchanged) say 1:200, and views rescales the plan's page_ratio / px_per_m."""
    from stoa.sheet.scalecheck import dimension_scale

    ok = _dim_sheet(str(tmp_path / "ok.pdf"), 100)
    d = dimension_scale(ok, 0, 100.0)
    assert d["agrees"] is True and d["n"] == 6 and abs(d["ratio"] - 100) < 1
    half = _dim_sheet(str(tmp_path / "half.pdf"), 200)
    d = dimension_scale(half, 0, 100.0)
    assert d["agrees"] is False and abs(d["factor"] - 2) < 0.03 and d["n_printed"] == 0
    assert dimension_scale(half, 0, None)["ratio"] == pytest.approx(200, rel=0.01)  # unscaled: the strings alone
    vs = [v for v in views(half) if v.ratio]
    assert vs and all(v.page_ratio == pytest.approx(200, rel=0.01) for v in vs)
    assert any("dimension strings measure 1:200" in n for v in vs for n in v.notes)
    vs = [v for v in views(ok) if v.ratio]
    assert vs and all(v.page_ratio == 100 for v in vs) and any("second length" in n for v in vs for n in v.notes)


@pytest.mark.parametrize("src,printed,f,want", [
    (A101F, 50.0, 0.5, 100.0),       # USACE A-101F 1:50 (mm) printed at half size
    (A101F, 50.0, 0.7071, 70.71),    # ... one A-size step smaller
    (".stoa/neutral/d1/816c77d1cf/816c77d1cf.pdf", 48.0, 0.5, 96.0),  # ft-in strings, 1/4" = 1'-0"
])
def test_dimension_scale_real_sheets_printed_smaller(tmp_path, src, printed, f, want):
    """VP c7 (xr-11): real CAD sheets resized as a 'fit to page' print would: the printed note stays, the dimension
    strings measure the true 1:N on the page; on the original they agree (0 false alarms on 149 upload-like PDFs)."""
    from stoa.sheet.scalecheck import dimension_scale

    need(src)
    assert dimension_scale(src, 0, printed)["agrees"] is True
    small = _resized(src, f, str(tmp_path / "small.pdf"))
    d = dimension_scale(small, 0, printed)
    assert d["agrees"] is False and d["ratio"] == pytest.approx(want, rel=0.02)
    vs = [v for v in views(small) if v.ratio == printed]
    assert vs and all(v.page_ratio == pytest.approx(want, rel=0.02) for v in vs)


def test_footprint_frame_rules_and_overall_dimension_check():
    """VP c7: on USACE A-101F (1:50, thin double-line walls) the line-pair closing turned the sheet's double frame into
    a ring and the 'building outline' was the whole page (1,175 m² @0.35). Frame / title-block rules outside the view
    (>= 70 % of the page long) are blanked; the outline that is left (still wider than the building: dimension chains)
    is checked on the overall dimension strings (26.9 x 15.5 m) and flagged at 0.3 with the bounding rectangle."""
    from stoa.sheet.footprint import footprint

    r = footprint(need(A101F), 0)
    assert r["gea_m2"] < 700 and r["confidence"] <= 0.3
    assert any("longest dimension string" in m and "upper bound" in m for m in r["missing"])
    assert any("second length" in a for a in r["assumptions"])


def test_gross_question_steps_aside_for_excluded_parts():
    """S0 23:55 (d30 wild-dev loss x1-140-farm): 'the footprint of the house without the porches' = the whole outline
    (216.1 m², gold 47.57): the outline cannot leave named parts out, so footprint does not take such questions."""
    from stoa.sheet.footprint import is_gross_area_question as g

    assert not g("What is the footprint of the house without the porches, in square metres?")
    assert not g("What is the total floor area excluding the garage?")
    assert g("What is the footprint of the house?") and g("What is the gross internal floor area of this plan?")


def test_dedupe_keeps_repeated_rooms_of_one_view():
    """G1 00:01: EFA colour sheet prints 'ICT Classroom 62 m2' twice (two rooms) -> both kept (total 124, not 62)."""
    from stoa.sheet.labels import as_area_tags

    t = as_area_tags(need(f"{NEUTRAL}/d7c5a1e97b/d7c5a1e97b.pdf"), "What is the total area of the ICT classrooms?")
    assert sorted(x["value"] for x in t if "ICT" in (x["name"] or "")) == [62.0, 62.0]


def test_storey_words_estonian():
    """T3 00:05 (Esplanades IFC plan sheets print 'Korruse plaan: 1.korrus'): Estonian levels, entrance floor = 1."""
    from stoa.sheet.sets import _storey_of

    assert _storey_of("Maleva tänav 18 korterelamu — Korruse plaan: 1.korrus")[0] == "0"
    assert _storey_of("korrus 3")[0] == "2" and _storey_of("Keldrikorrus")[0] == "-1"
    assert _storey_of("Katusekorrus")[0] == "attic" and _storey_of("Katuse plaan")[0] == "roof"
