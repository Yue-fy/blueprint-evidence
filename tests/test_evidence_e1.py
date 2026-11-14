"""E1: evidence precision (ground truth from SVG / DXF twins) and candidate evidence on raster plans."""

import pytest

from stoa.contract import Answer
from stoa.data import abspath
from stoa.evidence.check import check_item, expected_kinds, iou
from stoa.eval.evidence import evidence_metrics, is_exact

SWISS = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"
SWISS_PNG = "data/swiss_dwellings/site1000_bldg2082_floor9385.png"
IFC_PLAN = "data/ifc_models/bimspeed-barlad_ro-arc-m3-e-pth-0000_plan-02-ground-level.pdf"
CC = "data/cubicasa5k/high_quality-3638.png"
needs_data = pytest.mark.skipif(not abspath(SWISS).exists() or not abspath(IFC_PLAN).exists(),
                                reason="dataset not available")


def test_iou_and_kinds():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert expected_kinds("How many doors are on this floor?") == {"door"}
    assert "room" in expected_kinds("What is the area of room 2.01?")
    assert is_exact({"bbox": [0, 0, 1, 1], "note": "door 3"}, "pipeline/dxf")
    assert not is_exact({"bbox": [0, 0, 1, 1], "note": "toilet 1; cross-check: detector (pretrained CubiCasa5K)"}, "")
    assert not is_exact({"bbox": [0, 0, 1, 1], "note": "1"}, "pipeline/vlm_lora")
    assert is_exact({"bbox": [0, 0, 1, 1], "note": "x", "exact": True}, "pipeline/vlm_lora")


@needs_data
def test_swiss_door_box_is_right_and_shifted_box_is_off():
    from stoa.evidence.groundtruth import ground_truth

    gt = ground_truth(SWISS, "pdf_pt")
    assert gt is not None and gt.source == "swiss_dxf"
    door = next(o for o in gt.objects if o.kind == "door")
    b = list(door.box)
    q = "How many doors are drawn on this floor plan?"
    assert check_item({"file": SWISS, "page": 0, "bbox": b, "bbox_space": "pdf_pt"}, q).status == "right"
    # the same box padded by 1 pt (what the dxf solver draws) is still right
    p = [b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1]
    assert check_item({"file": SWISS, "page": 0, "bbox": p, "bbox_space": "pdf_pt"}, q).status == "right"
    # far outside the drawing: off; on a door but asked about windows: wrong object
    assert check_item({"file": SWISS, "page": 0, "bbox": [1, 1, 3, 3], "bbox_space": "pdf_pt"}, q).status == "off"
    assert check_item({"file": SWISS, "page": 0, "bbox": b, "bbox_space": "pdf_pt"},
                      "How many windows are drawn on this floor plan?").status == "wrong_object"
    # the PNG render: same object in image pixels
    gp = ground_truth(SWISS_PNG, "image_px")
    d2 = gp.by_ident[door.ident]
    assert check_item({"file": SWISS_PNG, "bbox": list(d2.box), "bbox_space": "image_px"}, q).status == "right"


@needs_data
def test_ifc_guid_box_must_match_svg():
    from stoa.evidence.groundtruth import ground_truth

    gt = ground_truth(IFC_PLAN, "pdf_pt")
    win = next(o for o in gt.objects if o.kind == "window")
    q = "How many windows are on this plan?"
    ev = {"file": IFC_PLAN, "page": 0, "bbox": list(win.box), "bbox_space": "pdf_pt", "ifc_guid": win.ident}
    assert check_item(ev, q).status == "right"
    moved = [win.box[0] + 50, win.box[1] + 50, win.box[2] + 50, win.box[3] + 50]
    assert check_item({**ev, "bbox": moved}, q).status == "off"
    rows = [{"question": q, "type": "count", "value": 1, "abstained": False, "solver": "pipeline/ifc",
             "evidence": [ev], "confidence": 0.9}]
    m = evidence_metrics(rows)
    assert m["ev_right"] == 1.0 and m["ev_count_exact"] == 1.0


def test_candidates_attach_only_to_evidence_less_answers(monkeypatch):
    from stoa.evidence import raster

    g = {"rooms": [{"id": "R1", "cls": "bath", "bbox": [0, 0, 10, 10], "label": "KH",
                    "fixtures": {"toilet": [[1, 1, 3, 3]], "sink": [[4, 4, 5, 5], [6, 6, 7, 7]]}},
                   {"id": "R2", "cls": "bedroom", "bbox": [10, 0, 20, 10], "label": "MH", "fixtures": {}}],
         "parts": []}
    monkeypatch.setattr(raster, "_graph", lambda path: g)
    a = Answer(value=1, unit="count", confidence=0.9, solver="vlm_lora", missing=["no located evidence from this solver"])
    tr: list[str] = []
    assert raster.attach_candidates(CC, "wc", a, tr)
    assert len(a.evidence) == 1 and "= the answer ✓" in a.evidence[0].note and "segmentation" in a.evidence[0].note
    assert a.confidence == 0.9 and "no located evidence from this solver" not in a.missing
    b = Answer(value=3, unit="count", confidence=0.6, solver="vlm_lora")
    assert raster.attach_candidates(CC, "sink", b, [])
    assert len(b.evidence) == 2 and "candidate" in b.evidence[0].note and "says 3" in b.evidence[0].note
    c = Answer(value=1, unit="count", confidence=0.6, solver="vlm_lora")
    assert raster.attach_candidates(CC, "storey", c, []) and c.evidence[0].bbox == [0.0, 0.0, 20.0, 10.0]
    d = Answer(value=1, unit="count", confidence=0.6, solver="prior")
    assert raster.attach_candidates(CC, "shower_bathtub", d, []) and "bathroom region R1" in d.evidence[0].note
    assert "✓" not in d.evidence[0].note  # bathrooms are where to look, not a count of showers
    # already has evidence / unknown target: untouched
    e = Answer(value=1, unit="count", confidence=0.6, solver="x", evidence=c.evidence)
    assert not raster.attach_candidates(CC, "wc", e, [])
    assert not raster.attach_candidates(CC, "parking", Answer(value=1, unit="count"), [])
    # every candidate note is drawn dashed by the UI / counted approximate by the metric
    assert not is_exact(b.evidence[0].model_dump(), "pipeline/prior")


def test_exact_field_and_marking():
    from stoa.contract import Evidence
    from stoa.evidence.exactness import classify, mark_exact

    # additive field, default None, JSON round trip
    e = Evidence(file="data/x.pdf", page=0, bbox=[0, 0, 1, 1], bbox_space="pdf_pt", note="door 3")
    assert e.exact is None and Evidence.model_validate({**e.model_dump(), "exact": True}).exact is True
    a = Answer(value=2, unit="count", solver="dxf", evidence=[
        e, e.model_copy(update={"note": "IfcSlab x | projected: not drawn on this plan"}),
        e.model_copy(update={"exact": False})])
    mark_exact(a)
    assert [x.exact for x in a.evidence] == [True, False, False]
    assert classify("toilet 1", "pipeline/vlm_lora", True, False) is False
    assert classify("text '1:100'", "pipeline/raster", True, False) is False  # VLM text-spotting box
    assert classify("IfcDoor D1", "pipeline/ifc", False, True) is True
    assert classify("something", "pipeline/agent", True, False) is None
    assert classify("file only", "pipeline/dxf", False, False) is None


def test_compositions_accept_every_named_target():
    k = expected_kinds("How many more doors than windows are shown on this floor plan?")
    assert {"door", "window"} <= k


@needs_data
def test_zero_count_shows_the_searched_area():
    from stoa.evidence.raster import attach_search_area

    a = Answer(value=0, unit="count", confidence=0.97, solver="dxf")
    assert attach_search_area(SWISS, "sauna", a, [])
    e = a.evidence[0]
    assert e.exact is False and e.bbox_space == "pdf_pt" and e.page == 0 and "no sauna found" in e.note
    assert e.bbox[2] > e.bbox[0] + 100  # the plan's extent, not a symbol
    assert a.confidence == 0.97 and a.value == 0
    b = Answer(value=2, unit="count", confidence=0.9, solver="dxf")
    assert not attach_search_area(SWISS, "sauna", b, []) and not b.evidence


@needs_data
def test_superlative_shows_what_it_beat():
    from stoa.solvers import get_solver

    a = get_solver("dxf").solve(SWISS, "Which apartment has the largest kitchen?")
    assert a.value == "apartment A"
    notes = [e.note for e in a.evidence]
    assert len(notes) >= 2 and not notes[0].startswith("compared:")  # the winner first (UI lights it)
    assert all(n.startswith("compared: ") for n in notes[1:])


def test_text_grounding_rules():
    from stoa.evidence.textground import _hits, _value_strings

    assert "12'-8" + chr(34) in _value_strings("12' - 8" + chr(34))  # both feet-inch spellings are searched
    assert _value_strings("unknown") == [] and _value_strings("yes") == [] and _value_strings(None) == []
    assert _value_strings(50.0)[0] == "50" and "36,42" in _value_strings(36.42)
    words = ((0, 0, 10, 5, "4000"), (11, 0, 20, 5, "PSI"), (30, 0, 40, 5, "(4000)"))
    hs = _hits(words, ["4000 PSI"])
    assert len(hs) == 1 and hs[0][0] == (0, 0, 20, 5)
    assert len(_hits(words, ["4000"])) == 2  # punctuation around a word is ignored


@needs_data
def test_room_boxes_get_their_outline_symbols_do_not():
    from stoa.contract import Evidence
    from stoa.evidence.groundtruth import ground_truth
    from stoa.evidence.shapes import add_polygons

    gt = ground_truth(SWISS, "pdf_pt")
    room = next(o for o in gt.objects if o.kind == "room" and o.polygon)
    door = next(o for o in gt.objects if o.kind == "door")
    pad = lambda b: [b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1]  # noqa: E731 — the dxf solver's 1 pt padding
    a = Answer(value=12.0, unit="m2", solver="dxf", evidence=[
        Evidence(file=SWISS, page=0, bbox=pad(room.box), bbox_space="pdf_pt", note="room"),
        Evidence(file=SWISS, page=0, bbox=pad(door.box), bbox_space="pdf_pt", note="door 1")])
    add_polygons(a)
    assert a.evidence[0].polygon and len(a.evidence[0].polygon) >= 3
    assert a.evidence[1].polygon is None
    assert Evidence.model_validate(a.evidence[0].model_dump()).polygon == a.evidence[0].polygon


def test_wall_axis_is_the_long_centre_line():
    from stoa.evidence.shapes import wall_axis

    ax = wall_axis([(0, 0), (100, 0), (100, 10), (0, 10)])
    xs = sorted(p[0] for p in ax)
    assert xs == [0, 100] and all(abs(p[1] - 5) < 1e-6 for p in ax)


@needs_data
def test_lone_ifc_plan_pdf_room_area_from_the_sheet():
    from stoa.evidence.ifcplan_pdf import room_area_answer, space_polygons

    pdf = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"
    sp = space_polygons(pdf)
    assert len(sp) >= 40 and all(s["scale"] == 100 for s in sp)
    a = room_area_answer(pdf, "What is the area of room KORIDOR 119?")
    assert a is not None and abs(a.value - 63.84) <= 0.02 * 63.84
    e = a.evidence[0]
    assert e.polygon and e.exact is True and "1:100" in a.assumptions[0]
    assert room_area_answer(pdf, "What is the total net side area of all walls?") is None


def test_scale_statement_by_solver():
    from stoa.evidence.shapes import add_scale_statement

    a = add_scale_statement(Answer(value=12.0, unit="m2", solver="pipeline/dxf", trace=["fit … scale 1:200"]))
    assert a.assumptions[0].startswith("scale: measured in the DXF") and "1:200" in a.assumptions[0]
    b = add_scale_statement(Answer(value=3.6, unit="m2", solver="pipeline/pdf_vector", trace=["scale 1:100 from the title block"],
                                   assumptions=["net floor area = area of the room's ROOM_OUTLINE polygon in the DXF (metres)"]))
    assert "1:100" in b.assumptions[0] and "DXF" not in b.assumptions[1]
    c = add_scale_statement(Answer(value=3, unit="count", solver="pipeline/dxf"))
    assert not c.assumptions  # counts need no scale
    d = add_scale_statement(Answer(value=20.0, unit="m2", solver="pipeline/vlm_direct"))
    assert not d.assumptions  # estimators state their own basis


@needs_data
def test_lone_ifc_plan_total_area_skips_walls():
    from stoa.evidence.ifcplan_pdf import total_area_answer

    pdf = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"
    a = total_area_answer(pdf, "What is the total net floor area of all rooms (spaces) on this floor, in m²?")
    assert a is not None and a.value > 63 and len(a.evidence) >= 10 and all(e.polygon for e in a.evidence)
    assert total_area_answer(pdf, "What is the total net side area of all walls on this floor?") is None


@needs_data
def test_lone_sheet_fills_give_plan_rooms_boxes_their_outline(tmp_path):
    """An uploaded IFC-plan PDF (no twin): plan_rooms reports a fill polygon's bounding box; the box gets the fill's
    outline back, is marked exact, and the answer states the scale it was measured at."""
    import shutil

    from stoa.contract import Evidence
    from stoa.evidence.exactness import mark_exact
    from stoa.evidence.ifcplan_pdf import room_area_answer
    from stoa.evidence.shapes import add_polygons, add_scale_statement

    src = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"
    up = tmp_path / "d1f2e3a4b5.pdf"
    shutil.copy(abspath(src), up)
    ref = room_area_answer(str(up), "What is the area of room KORIDOR 119?")
    assert ref is not None
    box = ref.evidence[0].bbox
    a = Answer(value=ref.value, unit="m2", confidence=0.9, solver="pipeline/plan_rooms",
               trace=["plan_rooms: 44 filled room polygons with labels (fill (0.89, 0.93, 0.97)); printed scale 1:100"],
               evidence=[Evidence(file=str(up), page=0, bbox=[round(v, 1) for v in box], bbox_space="pdf_pt",
                                  note="KORIDOR 119: 63.8 m²")])
    mark_exact(add_scale_statement(add_polygons(a)))
    e = a.evidence[0]
    assert e.polygon and len(e.polygon) >= 4 and e.exact is True
    assert a.assumptions[0].startswith("scale: 1:100 printed on the sheet")
    g = add_scale_statement(Answer(value=20.0, unit="m2", solver="pipeline/plan_rooms",
                                   trace=["plan_rooms: 9 …; no scale printed: 1:50 makes the 9 labelled rooms …"]))
    assert "NOT printed" in g.assumptions[0] and "1:50" in g.assumptions[0]


@needs_data
def test_plan_rooms_cross_check_caps_disagreement():
    from stoa.evidence.ifcplan_pdf import DISAGREE_CAP, check_plan_rooms, space_polygons

    pdf = str(abspath("data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"))
    n = len(space_polygons(pdf))
    q = "How many rooms (spaces) are shown on this floor plan?"
    ok = check_plan_rooms(Answer(value=n, unit="count", confidence=0.8, solver="pipeline/plan_rooms"), pdf, q)
    assert ok.confidence == 0.8 and any("cross-check ✓" in t for t in ok.trace)
    bad = check_plan_rooms(Answer(value=n - 3, unit="count", confidence=0.8, solver="pipeline/plan_rooms"), pdf, q)
    assert bad.confidence == DISAGREE_CAP and any(str(n) in m for m in bad.missing)
    other = check_plan_rooms(Answer(value=n - 3, unit="count", confidence=0.8, solver="pipeline/dxf"), pdf, q)
    assert other.confidence == 0.8  # only plan_rooms answers are checked
    a = check_plan_rooms(Answer(value=63.84, unit="m2", confidence=0.95, solver="plan_rooms"), pdf,
                         "What is the area of room KORIDOR 119?")
    assert a.confidence == 0.95 and any(x.startswith("checked:") for x in a.assumptions)


@needs_data
def test_metric_maps_an_upload_copy_to_its_dataset_file(tmp_path):
    import os

    from stoa.data import load_questions
    from stoa.eval.evidence import annotate

    q = next(q for q in load_questions() if q.path == IFC_PLAN)
    up = tmp_path / "e9f8.pdf"
    os.symlink(abspath(IFC_PLAN), up)  # the probes use hard links: same file, other name, no siblings
    from stoa.evidence.groundtruth import ground_truth

    gt = ground_truth(IFC_PLAN, "pdf_pt")
    o = next(o for o in gt.objects if o.kind == "window")
    row = {"qid": q.qid, "question": "How many windows are on this plan?", "path": str(up),
           "evidence": [{"file": str(up), "page": 0, "bbox": list(o.box), "bbox_space": "pdf_pt", "note": "IfcWindow"}]}
    assert annotate([row])[0]["ev_check"][0]["status"] == "right"


def test_d29_dxf_and_svg_bbox_spaces():
    from stoa.contract import Evidence

    for sp in ("pdf_pt", "image_px", "dxf", "svg"):
        e = Evidence(file="u/floor.dxf", bbox=[0, 0, 1, 1], bbox_space=sp, polygon=[[0, 0], [1, 0], [1, 1]])
        assert e.bbox_space == sp
    with pytest.raises(Exception):
        Evidence(file="u/floor.dxf", bbox=[0, 0, 1, 1], bbox_space="mm")


@needs_data
def test_lone_dxf_evidence_in_dxf_units(tmp_path):
    """D29: a DXF uploaded alone (no PDF/PNG to draw on) gets boxes + room outlines in the DXF's own units."""
    import shutil

    from stoa.evidence.check import check_item
    from stoa.evidence.shapes import add_polygons
    from stoa.solvers.vector.dxf_solver import DxfSolver

    up = tmp_path / "a1b2c3.dxf"
    shutil.copy(abspath(SWISS.replace(".pdf", ".dxf")), up)
    q = "What is the total area of all rooms?"
    a = add_polygons(DxfSolver(llm=0).solve(str(up), q))
    e = a.evidence[0]
    assert e.bbox_space == "dxf" and e.exact is True and e.polygon and len(e.polygon) >= 4
    assert check_item(e.model_dump(), q).status == "right"
    d = DxfSolver(llm=0).solve(SWISS, "How many doors are there?")  # with its PDF: unchanged
    assert d.evidence[0].bbox_space == "pdf_pt"


@needs_data
def test_lone_ifc_plan_svg_answers_with_svg_space_evidence(tmp_path):
    """Captain 12:52 (all formats): an IfcConvert plan .svg uploaded alone answers room areas, element counts and
    totals from the SVG itself, with evidence in bbox_space 'svg' (D29) that the metric can check."""
    import shutil

    from stoa.evidence.check import check_item
    from stoa.evidence.plan_svg import answer

    src = "data/ifc_models/kit-smiley-west_ac-20-smiley-west-10-bldg_plan-02-eg.svg"
    up = tmp_path / "7f3e2d.svg"
    shutil.copy(abspath(src), up)
    c = answer(str(up), "Wie viele Stützen sind im Grundriss „EG“ dargestellt?")  # columns drawn as <circle>
    assert c.value == 20 and c.confidence >= 0.8 and all(e.bbox_space == "svg" and e.exact for e in c.evidence)
    q = "How many doors are shown on this floor plan?"
    d = answer(str(up), q)
    assert d.value > 0 and check_item(d.evidence[0].model_dump(), q).status == "right"
    t = answer(str(up), "What is the total net floor area of all rooms (spaces) on this floor, in m²?")
    assert t.unit == "m2" and t.value > 50 and t.assumptions[0].startswith("scale: 1:100 printed")
    assert all(e.polygon for e in t.evidence)
    m = answer(str(up), "How many doors are there in the model 'Smiley West'?")
    assert m.confidence <= 0.3 and any("this sheet" in a for a in m.assumptions)  # one storey ≠ the whole model
    assert answer(str(up), "What is the total net volume of all walls, in m³?") is None
    assert answer(abspath(src.replace(".svg", ".pdf")), q) is None  # SVG only


def test_wet_room_fallback_when_no_symbol_of_the_kind(monkeypatch):
    """A WC count where the detector found no WC seat (or the bathroom was segmented 'undefined'): the wet rooms are
    shown as where to look, approximate, never as agreeing with the count."""
    from stoa.evidence import raster

    g = {"rooms": [{"id": "R1", "cls": "undefined", "bbox": [0, 0, 10, 10], "fixtures": {"sink": [[1, 1, 2, 2]]}},
                   {"id": "R2", "cls": "kitchen", "bbox": [10, 0, 20, 10], "fixtures": {"sink": [[11, 1, 12, 2]]}},
                   {"id": "R3", "cls": "bath", "bbox": [20, 0, 30, 10], "fixtures": {}}], "parts": []}
    monkeypatch.setattr(raster, "_graph", lambda path: g)
    b = Answer(value=1, unit="count", confidence=0.9, solver="pipeline/vlm_lora")
    assert raster.attach_candidates(CC, "bathroom", b, []) and [e.bbox for e in b.evidence] == [[20.0, 0.0, 30.0, 10.0]]
    for target in ("wc", "wc_room"):  # no WC seat symbol / no room labelled WC → the wet rooms
        a = Answer(value=2, unit="count", confidence=0.9, solver="pipeline/vlm_lora")
        assert raster.attach_candidates(CC, target, a, [])
        assert [e.bbox for e in a.evidence] == [[0.0, 0.0, 10.0, 10.0], [20.0, 0.0, 30.0, 10.0]]  # not the kitchen
        assert "wet room" in a.evidence[0].note and "✓" not in a.evidence[0].note and a.confidence == 0.9
        assert not is_exact(a.evidence[0].model_dump(), "pipeline/vlm_lora")


@needs_data
def test_room_length_width_perimeter_get_their_measured_line(tmp_path):
    """Captain 12:44: a length answer shows the measured line. A room's width / length / perimeter on an uploaded
    sheet: the polyline's length at the sheet scale equals the answer."""
    import shutil

    from stoa.evidence.shapes import add_dimension_lines, add_polygons
    from stoa.solvers.plan_rooms import PlanRooms

    pdf = tmp_path / "f00d12.pdf"
    shutil.copy(abspath("data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf"), pdf)
    k = 0.0254 / 72 * 100  # pt → m at the printed 1:100

    def length(pl):
        return sum(((pl[i][0] - pl[i + 1][0]) ** 2 + (pl[i][1] - pl[i + 1][1]) ** 2) ** 0.5 for i in range(len(pl) - 1))

    for q in ("What is the perimeter of room KORIDOR 119?", "How wide is room KORIDOR 119?",
              "How long is room KORIDOR 119?"):
        a = add_dimension_lines(add_polygons(PlanRooms().solve(str(pdf), q)), q)
        assert a.unit == "m" and len(a.evidence) == 1 and a.evidence[0].polygon
        assert abs(length(a.evidence[0].polyline) * k - a.value) <= 0.01 * a.value
    q = "What are the dimensions of room KORIDOR 119?"
    d = add_dimension_lines(add_polygons(PlanRooms().solve(str(pdf), q)), q)
    assert len(d.evidence) == 2 and all(e.polyline for e in d.evidence)


def test_sum_terms_add_up_and_printed_area_leads_the_note():
    """Ziying 13:23: every term of a sum is listed with the area that was SUMMED, so the evidence adds up."""
    from stoa.eval.evidence import _sum_adds_up
    from stoa.solvers.vector.dxf_solver import _room_note
    from stoa.solvers.vector.floor import Room

    r = Room.__new__(Room)
    r.__dict__.update({"area": 35.38, "printed_area": 29.03})
    r.label = lambda: "2.10 'Treppenhaus'"  # type: ignore[method-assign]
    assert _room_note(r).startswith("room 2.10 'Treppenhaus': 29.03 m² as printed (DXF polygon 35.38 m²")
    ev = [{"note": "room 2.09: 10.00 m² (plan prints 10.00 m² ✓)"}, {"note": _room_note(r)}]
    assert _sum_adds_up({"value": 39.03, "unit": "m2", "evidence": ev}) is True
    assert _sum_adds_up({"value": 45.38, "unit": "m2", "evidence": ev}) is False
    one = [{"note": "space 'A': 5.00 m²", "ifc_guid": "g1"}, {"note": "IfcSpace A: 5.00 m²", "ifc_guid": "g1"}]
    assert _sum_adds_up({"value": 5.0, "unit": "m2", "evidence": one}) is None  # one room shown in 2D and 3D


@needs_data
def test_text_answer_box_points_at_the_printed_value():
    """Stoa-style upload (a Revit sheet printing 'U120 - 1102 SF'): a number read from the sheet's text whose box is a
    sheet-wide table row gets a tight box on the value; a box without the value gets the printed place (SF ↔ m²)."""
    from stoa.contract import Evidence
    from stoa.evidence.textground import ground_printed_value

    f = str(abspath("data/opening_design/CTR/Most Current Set/Full size sheets/A101 - FLOOR PLANS - 1ST FLOOR.pdf"))
    if not abspath(f).exists():
        pytest.skip("sheet not available")

    def ans(v, unit, box):
        return Answer(value=v, unit=unit, confidence=0.85, solver="pipeline/pdf_text",
                      evidence=[Evidence(file=f, page=0, bbox=box, bbox_space="pdf_pt", note="t2 row: …", exact=True)])

    a = ans(102.38, "m2", [491, 445, 2662, 468])  # 102.38 m² = 1102 SF, printed in the wide row
    assert ground_printed_value(f, "How big is unit U120 in square metres?", a)
    e = a.evidence[0]
    assert e.exact and "1102" in e.note and e.bbox[2] - e.bbox[0] < 400 and len(a.evidence) == 2
    b = ans(680, "ft2", [10, 10, 50, 20])  # a box that does not hold the value
    assert ground_printed_value(f, "What is the area of unit U123?", b) and "'680' printed here" in b.evidence[0].note
    c = ans(680, "ft2", [10, 10, 50, 20])
    c.solver = "pipeline/dxf"  # geometry solvers' boxes are objects, not printed text: untouched
    assert not ground_printed_value(f, "What is the area of unit U123?", c)


def test_svg_ifc_answers_are_exact_and_state_their_scale():
    from stoa.contract import Evidence
    from stoa.evidence.exactness import mark_exact
    from stoa.evidence.shapes import add_scale_statement

    a = Answer(value=324.63, unit="m2", solver="pipeline/svg_ifc",
               trace=["scale 1:200 from 42 door symbols (40 with their width in the name; measured 1.02x the standard scale)"],
               evidence=[Evidence(file="u/x.svg", bbox=[0, 0, 5, 5], bbox_space="image_px", note="space A: 10.71 m²")])
    mark_exact(add_scale_statement(a))
    assert a.evidence[0].exact is True and a.assumptions[0].startswith("scale: 1:200 measured from 42 door symbols")


def test_printed_value_on_an_image_uses_the_text_layer_already_read(tmp_path):
    from PIL import Image

    from stoa.evidence.textground import ground
    from stoa.solvers.raster import textlayer as T

    img = tmp_path / "plan.jpg"
    Image.new("RGB", (2400, 1800), "white").save(img)
    p = str(img)
    assert ground(p, "How big is the largest apartment type?", 78.0, "m2") == []  # no layer read yet: no model call
    key = f"{p}{T.file_tag(p)}|ocr"
    T._mem[key] = T.TextLayer(path=p, size=(2400, 1800), items=[
        T.TextItem(text="OLOH.", box=[472, 602, 531, 620]),
        T.TextItem(text="62.5m2-62.0M2", box=[1139, 1714, 1578, 1782]),
        T.TextItem(text="78.0²-76.5", box=[1845, 1717, 2272, 1781])])
    try:
        ev = ground(p, "How big is the largest apartment type?", 78.0, "m2")  # Finnish sheet: no question word
        assert len(ev) == 1 and ev[0].bbox_space == "image_px" and ev[0].page is None and ev[0].exact is False
        x0, y0, x1, y1 = ev[0].bbox
        assert 1845 <= x0 < x1 <= 2100 and "'78.0' printed here" in ev[0].note
        assert ground(p, "How big is the largest apartment type?", 99.0, "m2") == []
    finally:
        T._mem.pop(key, None)


def _room_sheet(path):
    """Two rooms side by side drawn with lines only (1:100), a door gap in the left room's bottom wall."""
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    for a, b in [((100, 100), (400, 100)), ((100, 100), (100, 250)), ((400, 100), (400, 250)),
                 ((300, 100), (300, 250)), ((100, 250), (180, 250)), ((205, 250), (400, 250))]:
        sh.draw_line(a, b)
    sh.finish(color=(0, 0, 0), width=1.0)
    sh.commit()
    pg.insert_text((180, 175), "Room A 23.0 m2", fontsize=6)
    pg.insert_text((330, 175), "Room B 11.5 m2", fontsize=6)
    doc.save(path)


def test_linework_outline_is_kept_only_when_its_area_matches_the_printed_one(tmp_path):
    from shapely.geometry import Polygon

    from stoa.evidence.linework import PT_TO_M_AT_1, area_tags, outline_for_area

    f = str(tmp_path / "rooms.pdf")
    _room_sheet(f)
    tags = area_tags(f, 0)
    assert [round(t[0], 1) for t in tags] == [23.0, 11.5]
    room_a = 199 * 149 * (100 * PT_TO_M_AT_1) ** 2  # inside the 1 pt lines: ~36.9 m² at 1:100
    r = outline_for_area(f, 0, (200, 173), room_a, 100)
    assert r is not None and abs(r["area_m2"] / room_a - 1) <= 0.06 and r["support"] >= 2
    P = Polygon(r["polygon"])
    assert P.contains(Polygon([(110, 110), (290, 110), (290, 240), (110, 240)])) and P.bounds[2] <= 302
    assert outline_for_area(f, 0, (200, 173), room_a * 1.5, 100) is None  # printed area disagrees: no outline
    assert outline_for_area(f, 0, (200, 173), room_a, 150) is None  # wrong scale: no outline


def test_composed_answers_keep_each_parts_cross_check_line():
    from stoa.data import abspath
    from stoa.solvers import get_solver

    f = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"
    if not abspath(f).exists():
        pytest.skip("sheet not available")
    a = get_solver("pipeline").solve(f, "Wie viele Türen und Fenster sind insgesamt auf diesem Plan?")
    assert a.value == 64 or str(a.value) == "64"
    assert any(t.startswith("door: cross-check:") and "✓" in t for t in a.trace)  # W5 14:04: the UI's 'CAD agrees'


def test_tiny_space_whose_label_is_printed_next_to_it_gets_that_label():
    from stoa.evidence.ifcplan_pdf import room_area_answer

    f = ".stoa/human/drawings/dcaf5de24f8-dade23ec.pdf"
    from pathlib import Path

    if not Path(f).exists():
        pytest.skip("drawing not available")
    a = room_area_answer(f, "What is the net floor area of the room 'Storage (414B-19-001-009)' on '1. Level', in m²?")
    assert a is not None and a.value == 0.52 and a.evidence[0].polygon  # H 14:17: label outside its 0.52 m² fill


def test_measure_room_on_a_line_only_sheet_without_printed_areas(tmp_path):
    from stoa.evidence.linework import measure_room

    f = str(tmp_path / "rooms.pdf")
    _room_sheet(f)  # room A: wall lines 200 x 150 pt apart (centre to centre) = 7.06 x 5.29 m at 1:100 = 37.34 m²
    r = measure_room(f, 0, (200, 173), 100)
    assert r is not None and abs(r["area_m2"] / 37.34 - 1) <= 0.03, r
    assert abs(r["sides_m"][0] / 7.056 - 1) <= 0.02  # the side across a door opening bulges (+5 %): known, see NEXT
    assert measure_room(f, 0, (200, 173), 0) is None


def test_ifc_room_sum_names_the_models_own_area_when_it_differs_from_the_footprint():
    from pathlib import Path

    from stoa.solvers import get_solver

    f = ".stoa/neutral/x1/9b9959ce8f/9b9959ce8f.ifc"  # X1 wild: ArchiCAD house, sloped roof upstairs
    if not Path(f).exists():
        pytest.skip("model not available")
    a = get_solver("pipeline").solve(f, "How many square metres do the two bedrooms upstairs have together?")
    assert a.confidence <= 0.6 and any("'horizontal area'" in x and "17.32" in x for x in a.assumptions)
    assert any("convention" in x and "footprint" in x for x in a.missing)
    b = get_solver("pipeline").solve(".stoa/neutral/x1/6db2f9aa5b/6db2f9aa5b.ifc", "How big is the bedroom?")
    assert abs(float(b.value) - 18.136) <= 0.01 and "model area" in b.evidence[0].note  # X1 x1-009 gold (sloped roof)


def test_ifc_generic_larger_area_keeps_the_footprint():
    """S0 d15: Revit's generic 'Area' is 2-10 % LARGER than the footprint on the dataset clinic; Stoa's gold = footprint."""
    from pathlib import Path

    from stoa.solvers import get_solver

    f = ".stoa/neutral/p1/ifc/12d8a9c56f/12d8a9c56f.ifc"
    if not Path(f).exists():
        pytest.skip("model not available")
    a = get_solver("pipeline").solve(f, "What is the net floor area of the room 'CORRIDOR (1EC4)' on 'First Floor', in m²?")
    assert abs(float(a.value) - 8.46) <= 0.02 * 8.46
    assert a.confidence > 0.6  # not a conflict: just another convention, named in the assumptions


def _line_only_sheet(path):
    """Two rooms drawn as wall LINES only (no fills, no printed areas) at a printed 1:100: room 1 'CONFERENCE' / 'ROOM'
    (stacked label) 200 x 150 pt with a door in the shared wall drawn CAD-style as ONE path (arc of short segments +
    leaf + doorway chord); room 2 'COMMS ROOM' 100 x 150 pt."""
    import math

    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    for a, b in [((100, 100), (400, 100)), ((100, 250), (400, 250)), ((100, 100), (100, 250)), ((400, 100), (400, 250)),
                 ((300, 100), (300, 160)), ((300, 190), (300, 250))]:
        sh.draw_line(a, b)
    sh.finish(color=(0, 0, 0), width=1.0)
    # door in the shared wall x=300, opening y 160..190 (30 pt = 1.06 m), hinge (300, 190), swinging into room 1
    pts = [(300 - 30 * math.sin(t * math.pi / 2 / 20), 190 - 30 * math.cos(t * math.pi / 2 / 20)) for t in range(21)]
    for p, q in zip(pts, pts[1:]):
        sh.draw_line(p, q)
    sh.draw_line(pts[-1], (300, 190))  # leaf (open, perpendicular to the wall)
    sh.draw_line((300, 190), pts[0])  # doorway chord
    sh.finish(color=(0, 0, 0), width=0.3)
    sh.commit()
    pg.insert_text((180, 170), "CONFERENCE", fontsize=7)
    pg.insert_text((190, 178), "ROOM", fontsize=7)
    pg.insert_text((325, 175), "COMMS ROOM", fontsize=6)
    pg.insert_text((450, 380), "SCALE 1:100", fontsize=8)
    doc.save(path)


def test_lineplan_measures_a_labelled_room_on_a_line_only_sheet(tmp_path):
    from stoa.evidence.linework import PT_TO_M_AT_1, _door_arcs, _page
    from stoa.evidence.lineplan import answer, labels

    f = str(tmp_path / "lines.pdf")
    _line_only_sheet(f)
    assert any(lab["text"] == "CONFERENCE ROOM" for lab in labels(f))  # stacked lines = one label
    spokes, arcs = _door_arcs(_page(f, 0), 100)
    assert len(spokes) == 2 and arcs  # the door path's arc run found inside the closed polyline
    k = 100 * PT_TO_M_AT_1
    a = answer(f, "How big is the conference room?")
    assert a is not None and a.unit == "m2" and abs(a.value / (200 * 150 * k * k) - 1) <= 0.03, a
    assert a.evidence[0].polygon and a.evidence[0].exact is False and a.confidence <= 0.6
    assert a.assumptions[0].startswith("scale: 1:100 printed")  # the door swing's sector stays in the room
    b = answer(f, "How long is the comms room, wall to wall?")
    assert b is not None and b.unit == "m" and abs(b.value / (150 * k) - 1) <= 0.02 and b.evidence[0].polyline
    assert answer(f, "How many rooms are there?") is None and answer(f, "How thick is the outer wall?") is None


def test_lineplan_picks_a_room_by_its_position():
    from stoa.evidence.lineplan import _pick, _position

    rooms = [{"pt": (10, 10), "bbox": [0, 0, 20, 20]}, {"pt": (10, 30), "bbox": [0, 20, 20, 40]},
             {"pt": (10, 50), "bbox": [0, 40, 20, 60]}, {"pt": (50, 10), "bbox": [40, 0, 60, 20]}]
    assert _pick(rooms, _position("the room at the top right of the plan"))[0] is rooms[3]
    assert _pick(rooms, _position("the corner room at the top left"))[0] is rooms[0]
    assert _pick(rooms, _position("How big is the second room from the top on the left side?"))[0] is rooms[1]
    assert _pick(rooms, _position("Wie groß ist das dritte Zimmer von oben auf der linken Seite?"))[0] is rooms[2]
    assert _position("How big is the comms room?") is None


def test_printed_area_gets_the_traced_outline_as_a_cross_check(tmp_path):
    from stoa.contract import Answer, Evidence
    from stoa.evidence.lineplan import add_traced_outlines

    f = str(tmp_path / "lines.pdf")
    _line_only_sheet(f)  # conference room 200 x 150 pt at 1:100 = 37.34 m²

    def ans(printed):
        return Answer(value=printed, unit="m2", confidence=0.9, solver="pipeline/area_tags",
                      evidence=[Evidence(file=f, page=0, bbox=[180, 162, 229, 180], bbox_space="pdf_pt", exact=True,
                                         note=f"CONFERENCE ROOM: {printed} m²")])

    a = add_traced_outlines(ans(37.3))
    assert len(a.evidence) == 2 and a.evidence[1].polygon and a.evidence[1].exact is False
    assert a.value == 37.3 and a.confidence == 0.9 and "✓" in a.trace[-1]
    b = add_traced_outlines(ans(25.0))  # printed area disagrees with every traced outline: nothing added
    assert len(b.evidence) == 1 and not b.trace


def test_lineplan_wall_thickness_between_rooms_and_outer_walls(tmp_path):
    import pymupdf

    from stoa.evidence.lineplan import answer

    f = str(tmp_path / "walls.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    # outer walls: two face lines 10 pt apart (0.353 m at 1:100); a partition at x=300 drawn as two lines 6 pt apart
    for r in [(90, 90, 410, 260), (100, 100, 400, 250)]:
        sh.draw_rect(pymupdf.Rect(*r))
    for x in (297, 303):
        sh.draw_line((x, 100), (x, 250))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    pg.insert_text((170, 175), "KITCHEN", fontsize=7)
    pg.insert_text((330, 175), "BATHROOM", fontsize=7)
    pg.insert_text((450, 380), "SCALE 1:100", fontsize=8)
    doc.save(f)
    a = answer(f, "How thick is the wall between the kitchen and the bathroom?")
    assert a is not None and abs(a.value - 6 * 100 * 0.0254 / 72) <= 0.01 and a.evidence[0].polyline and a.confidence == 0.35, a
    b = answer(f, "How thick are the exterior walls?")
    assert b is not None and abs(b.value - 10 * 100 * 0.0254 / 72) <= 0.01 and b.confidence == 0.5, b


def test_lineplan_counts_rooms_by_their_printed_labels(tmp_path):
    import pymupdf

    from stoa.evidence.lineplan import answer

    f = str(tmp_path / "house.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    for i, (x, y, t) in enumerate([(100, 100, "BED ROOM 1"), (250, 100, "BED ROOM 2"), (400, 100, "KITCHEN"),
                                   (100, 250, "WC"), (250, 250, "WC"), (400, 250, "MEETING ROOM")]):
        pg.insert_text((x, y), t, fontsize=8)
    doc.save(f)
    assert answer(f, "How many bedrooms are there?").value == 2
    a = answer(f, "How many toilets (WC) are there?")
    assert a.value == 2 and len(a.evidence) == 2 and all(e.exact for e in a.evidence)
    assert answer(f, "Wie viele Schlafzimmer gibt es?").value == 2
    assert answer(f, "How many meeting rooms are on this level?").value == 1
    assert answer(f, "How many rooms are there?") is None and answer(f, "How many garages are there?") is None
    g = str(tmp_path / "notes.pdf")  # X1 15:51: a label STATING a count is read by a text reader, not counted as 1
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    pg.insert_text((100, 100), "3 BEDROOMS", fontsize=8)
    pg.insert_text((250, 100), "2 x WC", fontsize=8)
    doc.save(g)
    assert answer(g, "How many bedrooms are there?") is None and answer(g, "How many toilets are there?") is None
    assert answer(g, "How many risers does the main staircase have?") is None


def test_counted_elements_at_one_spot_say_so():
    from stoa.contract import Answer, Evidence
    from stoa.evidence.shapes import mark_stacked

    ev = [Evidence(file="a.pdf", page=0, bbox=[1, 1, 5, 5], bbox_space="pdf_pt", note=f"door: door {i}") for i in (1, 2)]
    ev.append(Evidence(file="a.pdf", page=0, bbox=[9, 9, 12, 12], bbox_space="pdf_pt", note="door: door 3"))
    a = mark_stacked(Answer(value=3, unit="count", confidence=0.9, evidence=ev))
    assert a.value == 3 and len(a.evidence) == 3 and "x2 at this spot" in a.evidence[0].note
    assert "spot" not in a.evidence[2].note and "3 items on 2 marks" in a.trace[-1]


def test_dxfplan_traces_rooms_between_wall_lines_of_a_lone_dxf(tmp_path):
    """A DXF with wall LINES + room-name TEXT and no room outlines (X1 dyimba): rooms measured on a sheet drawn from
    it; $INSUNITS says mm but the door block is 0.9 units wide -> metres; evidence back in DXF units."""
    ezdxf = pytest.importorskip("ezdxf")
    from stoa.contract import Answer
    from stoa.evidence import dxfplan

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4  # wrong on purpose (drawn in metres)
    msp = doc.modelspace()
    for x0, y0, x1, y1 in ((0, 0, 8.3, 5.3), (0.15, 0.15, 4.0, 5.15), (4.15, 0.15, 8.15, 5.15)):
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "A-Wall"})
    for k in range(6):  # a few extra wall stubs so the file has a wall layer worth reading
        msp.add_line((8.3 + k, 0), (8.3 + k, 0.1), dxfattribs={"layer": "A-Wall"})
    blk = doc.blocks.new("Door-90")
    blk.add_line((0, 0), (0.9, 0))
    blk.add_arc((0, 0), 0.9, 0, 90)
    msp.add_blockref("Door-90", (5, 0.15), dxfattribs={"layer": "A-Door"})
    msp.add_blockref("Door-90", (6, 0.15), dxfattribs={"layer": "A-Door"})
    msp.add_text("KITCHEN", dxfattribs={"layer": "A-Text", "height": 0.2}).set_placement((2.0, 2.6))
    msp.add_text("BEDROOM", dxfattribs={"layer": "A-Text", "height": 0.2}).set_placement((6.0, 2.6))
    f = str(tmp_path / "house.dxf")
    doc.saveas(f)
    sh = dxfplan.sheet(f)
    assert sh is not None and sh["unit_m"] == 1.0
    a = dxfplan.answer(f, "How big is the kitchen?")
    assert a is not None and a.value == pytest.approx(3.85 * 5.0, rel=0.02)
    e = a.evidence[0]
    assert e.bbox_space == "dxf" and e.file == f and 0 <= e.bbox[0] < 1 and 3.5 < e.bbox[2] < 4.5
    weak = Answer(value=0, unit="m2", confidence=0.96, solver="pipeline/dxf")
    b = dxfplan.replace_nothing_found(f, "How many square metres is the bedroom?", weak)
    assert b.solver == "pipeline/cad_lines" and b.value == pytest.approx(4.0 * 5.0, rel=0.02)
    good = Answer(value=12.5, unit="m2", confidence=0.95, solver="pipeline/dxf")
    assert dxfplan.replace_nothing_found(f, "How big is the kitchen?", good) is good


def test_lineplan_skirting_is_the_outline_minus_the_door_way(tmp_path):
    """Captain 16:15 / Stoa kind 2: skirting (baseboard) = traced outline minus the openings in the walls; a doorway
    chord DRAWN as a line (CAD door path: arc + leaf + chord) is still an opening; 'how many linear metres' is not a
    count; 'the conference room and the comms room' sums both rooms (the shared door way is cut from each)."""
    from stoa.evidence.linework import PT_TO_M_AT_1
    from stoa.evidence.lineplan import answer

    f = str(tmp_path / "lines.pdf")
    _line_only_sheet(f)
    k = 100 * PT_TO_M_AT_1
    a = answer(f, "How many linear metres of skirting board does the conference room need?")
    assert a is not None and a.unit == "m", a
    assert abs(a.value / ((700 - 30) * k) - 1) <= 0.02, (a.value, (700 - 30) * k, a.trace)
    ops = [e for e in a.evidence if "opening" in e.note and "no skirting" in e.note]
    assert len(ops) == 1 and abs(float(ops[0].note.split("opening ")[1].split(" m")[0]) / (30 * k) - 1) <= 0.1
    assert any(e.polyline and e.polygon for e in a.evidence) and all(e.exact is False for e in a.evidence)
    b = answer(f, "How much baseboard do the conference room and the comms room need in total?")
    assert b is not None and abs(b.value / ((700 - 30 + 500 - 30) * k) - 1) <= 0.03, (b.value, b.trace)
    assert answer(f, "How many metres of skirting does the kitchen need?") is None  # no such room on the sheet
    w = answer(f, "How many linear metres of skirting are needed for the whole floor?")
    assert w is not None and abs(w.value / ((700 - 30 + 500 - 30) * k) - 1) <= 0.03 and w.confidence <= 0.3, w
    assert answer(f, "How much skirting does the ballroom need on this floor?") is None  # named, not on the sheet


def test_printed_area_table_answers_totals_types_and_wings(tmp_path):
    """Captain 16:15 (X1 stoa-like AFCEC dorm A-2): a sheet's own area table 'LABEL = value SF' answers first: a TOTAL
    row named by the question, else the rows of the asked type (summed across wings), in the unit asked."""
    import pymupdf

    from stoa.evidence.areatable import answer, prefer_printed_table, rows

    f = str(tmp_path / "table.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    table = [("WEST WING DORMITORY =", "6,095 SF"), ("WEST WING CIRCULATION =", "914 SF"),
             ("TOTAL WEST WING =", "7,009 SF"), ("NORTH WING DORMITORY =", "6,285 SF"),
             ("NORTH WING CIRCULATION =", "1,403 SF"), ("TOTAL NORTH WING =", "7,688 SF"),
             ("COMMON SPACE =", "2,911 SF"), ("TOTAL SECOND FLOOR =", "17,608 SF")]
    for i, (lab, val) in enumerate(table):
        pg.insert_text((60, 60 + 14 * i), lab, fontsize=8)
        pg.insert_text((240, 60 + 14 * i), val, fontsize=8)
    pg.insert_text((400, 300), "ROOM 101", fontsize=8)
    pg.insert_text((400, 310), "250 SF", fontsize=8)  # a stacked room tag: not a table row
    doc.save(f)
    rs = rows(f)
    assert len(rs) == 8 and ("TOTAL WEST WING", 7009.0, "ft2") == rs[2][:3]
    a = answer(f, "What is the total area of the second floor in square metres?")
    assert a is not None and a.unit == "m2" and abs(a.value - 17608 * 0.09290304) < 0.01 and a.evidence[0].exact
    b = answer(f, "What is the total dormitory area across both wings?")
    assert b is not None and b.value == 6095 + 6285 and len(b.evidence) == 2
    assert answer(f, "How many square feet of circulation does the north wing have?").value == 1403
    assert answer(f, "What is the total area of the west wing?").value == 7009
    # H 19:23: 'm²' in the question and possessives ('the west wing's') used to find no row
    w = answer(f, "What is the total area of the west wing in m²?")
    assert w is not None and w.unit == "m2" and abs(w.value - 7009 * 0.09290304) < 0.01
    assert answer(f, "What is the west wing's total area in m²?") is not None
    assert answer(f, "How big is the north wing's dormitory?").value == 6285
    assert answer(f, "How many rooms are on this floor?") is None
    from stoa.contract import Answer

    kept = prefer_printed_table(f, "What is the total area of the west wing?", Answer(value=7009, unit="ft2",
                                                                                      confidence=0.6, solver="x"))
    assert kept.solver == "x" and any("printed area table" in t for t in kept.trace) and kept.confidence == 0.85
    assert kept.evidence and kept.evidence[0].exact
    rep = prefer_printed_table(f, "What is the total area of the west wing?", Answer(value=1580.2, unit="m2",
                                                                                     confidence=0.6, solver="x"))
    assert rep.value == 7009 and rep.solver == "area_table" and rep.confidence == 0.85


def test_slab_area_by_type_ignores_joints_and_dimension_lines(tmp_path):
    """Captain 16:15 (d) / Stoa kind 4: 'area of the 150 mm slab with D13 @ 300' = the region around its printed
    callout; a control joint (labelled CJ) and a dimension line across the slab do not split it; another type's slab
    next to it (behind a 0.3 m beam = two lines) stays out."""
    import pymupdf

    from stoa.evidence.linework import PT_TO_M_AT_1
    from stoa.evidence.slabs import answer

    f = str(tmp_path / "slab.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    sh = pg.new_shape()
    # slab A: x 100..500, y 100..300 pt; beam (two lines 0.3 m = 6 pt apart at 1:50) then slab B: x 506..700
    for a, b in [((100, 100), (700, 100)), ((100, 300), (700, 300)), ((100, 100), (100, 300)), ((700, 100), (700, 300)),
                 ((500, 100), (500, 300)), ((506, 100), (506, 300))]:
        sh.draw_line(a, b)
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.draw_line((300, 100), (300, 300))  # control joint
    sh.finish(color=(0, 0, 0), width=2.0)
    sh.draw_line((100, 200), (500, 200))  # a dimension line across slab A, its number beside it
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.commit()
    pg.insert_text((305, 150), "CJ", fontsize=6)
    pg.insert_text((280, 196), "7055", fontsize=6)
    pg.insert_text((150, 140), "150 SLAB-ON-GRADE W/ D13 @ 300", fontsize=6)
    pg.insert_text((560, 250), "300 SLAB-ON-GRADE W/ D16 @ 300", fontsize=6)
    pg.insert_text((600, 560), "SCALE 1:50", fontsize=8)
    doc.save(f)
    k = 50 * PT_TO_M_AT_1
    a = answer(f, "What is the area of the 150 mm slab-on-grade with D13 @ 300?")
    assert a is not None and a.unit == "m2" and abs(a.value / (400 * 200 * k * k) - 1) <= 0.03, (a and a.value, a)
    assert a.evidence[0].polygon and a.evidence[0].exact is False and a.confidence <= 0.45
    b = answer(f, "How big is the 300 mm slab with D16 bars?")
    assert b is not None and abs(b.value / (194 * 200 * k * k) - 1) <= 0.04, b and b.value
    assert answer(f, "What is the area of the 200 mm slab?") is None  # no such callout
    t = answer(f, "How thick is the slab reinforced with D13 bars?")
    assert t is not None and (t.value, t.unit) == (0.15, "m") and t.evidence[0].exact
    assert answer(f, "How many different slab types are called out on the plan?").value == 2
    pn = answer(f, "Into how many panels do the control joints (CJ) divide the 150 mm D13 slab?")
    assert pn is not None and (pn.value, pn.unit) == (2, "count") and len(pn.evidence) == 2


def test_slab_area_on_a_picture_traced_into_lines(tmp_path):
    """E1 cycle 9 (H 18:54, QA 18:56: Stoa's S-100F as PNG lost every slab answer): on P1's traced copy of a picture
    (one pen for every line, invisible OCR text) the callout's OCR lines are stacked into one block, a DASHED slab
    edge (real gaps; a PDF strokes it whole) still bounds the slab, and two OCR'd numbers that equal the traced sides
    become the answer (their product, conf 0.6)."""
    import pymupdf

    from stoa.evidence import slabs

    f = str(tmp_path / "plan.s6000.vec6.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    sh = pg.new_shape()
    # slab A: x 100..400, y 100..300 pt (1:50 -> 5.29 x 3.53 m); slab B to its right up to x 700; A's right edge dashed
    for a, b in [((100, 100), (700, 100)), ((100, 300), (700, 300)), ((100, 100), (100, 300)), ((700, 100), (700, 300))]:
        sh.draw_line(a, b)
    y = 100.0
    while y < 300:
        sh.draw_line((400, y), (400, min(300.0, y + 12)))
        y += 17  # 5 pt gaps
    sh.finish(color=(0, 0, 0), width=0.4)
    sh.commit()
    for (x, yy), t in [((150, 150), "150 SLAB-ON-GRADE W/"), ((150, 158), "D13 @ 300"), ((150, 166), "TOP OF SLAB"),
                       ((550, 250), "300 SLAB-ON-GRADE W/ D16 @ 300"), ((240, 75), "5290"), ((440, 200), "3530"),
                       ((600, 560), "SCALE 1:50")]:
        pg.insert_text((x, yy), t, fontsize=6, render_mode=3)
    doc.save(f)
    with pymupdf.open(f) as d:
        assert slabs.ocr_layer(d[0])
        texts = [b[4] for b in slabs.text_blocks(d[0])]
    assert any("150 SLAB-ON-GRADE W/" in t and "D13 @ 300" in t for t in texts), texts
    assert slabs._traced(f, 0)
    a = slabs.answer(f, "What is the area of the 150 SLAB-ON-GRADE W/ D13 @ 300?")
    assert a is not None and a.value == round(5290 * 3530 / 1e6, 3) and a.confidence == 0.6, (a and a.value, a)
    assert [e.exact for e in a.evidence[:2]] == [False, False]  # OCR'd number boxes are approximate


def test_door_arcs_exploded_into_segments_count_and_grid_bubbles_do_not(tmp_path):
    """USACE A-101F (1:50): door swings drawn as ~13 separate one-segment paths, and 0.4 m circles (grid bubbles /
    section markers, 4 quarter arcs on one centre) — only the 0.9 m swing is a door (E1 cycle 7)."""
    import math

    import pymupdf

    from stoa.evidence.linework import PT_TO_M_AT_1, door_radii_m

    f = str(tmp_path / "doors.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    r = 0.9 / (50 * PT_TO_M_AT_1)  # 0.9 m at 1:50 in pt
    cx, cy = 300.0, 250.0
    pts = [(cx - r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a))) for a in range(0, 91, 7)]
    for a, b in zip(pts, pts[1:]):  # one path per segment
        sh = pg.new_shape()
        sh.draw_line(a, b)
        sh.finish(color=(0, 0, 0), width=0.5, closePath=False)
        sh.commit()
    sh = pg.new_shape()
    for x in (80, 160, 460, 540):  # grid bubbles: full circles
        sh.draw_circle((x, 60), 0.4 / (50 * PT_TO_M_AT_1))
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.commit()
    doc.save(f)
    radii = door_radii_m(f, 0, 50)
    assert len(radii) == 1 and abs(radii[0] - 0.9) <= 0.03, radii


def test_room_nearest_a_named_landmark():
    """Ziying's dfe-02 'the Secondary Classroom nearest the north stair': the northmost stair label (up on the sheet),
    then the classroom label nearest to it; an unnamed subject ('the room next to the kitchen') is not guessed."""
    from stoa.evidence.lineplan import _NEAR_Q, _nearest_to

    def lab(t, x, y):
        return {"text": t, "words": t.lower().split(), "pt": (x, y), "bbox": [x - 5, y - 2, x + 5, y + 2]}

    labs = [lab("Stair", 332, 247), lab("Stair", 243, 841), lab("Secondary Classroom", 364, 303),
            lab("Secondary Classroom", 205, 303), lab("Secondary Classroom", 368, 634)]
    rooms = labs[2:]
    q = "What is the floor area of the Secondary Classroom nearest the north stair?"
    m = _NEAR_Q.search(q)
    pick, why = _nearest_to(labs, rooms, m.group(2))
    assert pick is labs[2] and "north taken as up" in why
    pick, _ = _nearest_to(labs, rooms, "south stair")
    assert pick is labs[4]
    assert _nearest_to(labs, rooms, "stair")[0] is None  # two stairs, no word says which


def test_printed_dimensions_pair_with_their_lines_and_span_a_rectangle(tmp_path):
    """dimrect (E1 cycle 7): a number is a dimension when its own line (between arrow tips) measures it at 1:N; two of
    them span the rectangle round a point (S-100F genset 4272 x 6200, A-101F toilet 1900 x 2054)."""
    import pymupdf

    from stoa.evidence import dimrect as D
    from stoa.evidence.linework import PT_TO_M_AT_1

    f = str(tmp_path / "dims.pdf")
    k = 1000 * 50 * PT_TO_M_AT_1  # mm per pt at 1:50
    w_pt, h_pt = 4272 / k, 6200 / k
    x0, y0 = 150.0, 120.0
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    sh = pg.new_shape()
    sh.draw_rect(pymupdf.Rect(x0, y0, x0 + w_pt, y0 + h_pt))  # the slab / room outline
    sh.finish(color=(0, 0, 0), width=1.0)
    yd, xd = y0 + h_pt + 20, x0 + w_pt + 20
    sh.draw_line((x0 + 6, yd), (x0 + w_pt - 6, yd))  # horizontal dimension line, stops at its arrow bases
    sh.draw_line((xd, y0 + 6), (xd, y0 + h_pt - 6))
    for a, b in (((x0, yd), (x0 + w_pt, yd)), ((xd, y0), (xd, y0 + h_pt))):
        sh.draw_line(a, (a[0], a[1] - 25) if a[1] == b[1] else (a[0] - 25, a[1]))  # extension lines
    sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
    for tri in ([(x0, yd), (x0 + 6, yd - 1.5), (x0 + 6, yd + 1.5)], [(x0 + w_pt, yd), (x0 + w_pt - 6, yd - 1.5),
                (x0 + w_pt - 6, yd + 1.5)], [(xd, y0), (xd - 1.5, y0 + 6), (xd + 1.5, y0 + 6)],
                [(xd, y0 + h_pt), (xd - 1.5, y0 + h_pt - 6), (xd + 1.5, y0 + h_pt - 6)]):
        sh.draw_polyline(tri)
        sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.1, closePath=True)
    sh.commit()
    pg.insert_text((x0 + w_pt / 2 - 10, yd - 3), "4272", fontsize=8)
    pg.insert_text((xd - 3, y0 + h_pt / 2 + 10), "6200", fontsize=8, rotate=90)
    pg.insert_text((x0 + 20, y0 + 20), "1234", fontsize=8)  # a number on no line: not a dimension
    doc.save(f)
    ds = D.dims(f, 0, 50.0)
    assert sorted(d[0] for d in ds) == [4272, 6200] and all(abs(d[6] - d[0]) <= 0.01 * d[0] for d in ds), ds
    r = D.rect_at(f, 0, 50.0, (x0 + 40, y0 + 60))
    assert r is not None and r["area_m2"] == round(4272 * 6200 / 1e6, 3)
    assert D.rect_spanning(f, 0, 50.0, (x0 + 1, y0 - 1, x0 + w_pt - 1, y0 + h_pt + 1)) is not None
    assert D.rect_spanning(f, 0, 50.0, (x0 + 30, y0, x0 + w_pt, y0 + h_pt)) is None  # a smaller outline: not spanned


def test_area_answered_with_a_printed_extent_string_becomes_its_product():
    """D1 16:48 (AW 078-24-29 S-201): the raster reader returns the callout's extent '9'-0' x 9'-11'' for 'what is the
    area of the 1'-9" slab'; imperial slab callouts parse as thickness + bars."""
    from stoa.contract import Answer
    from stoa.evidence.slabs import _BAR, _IMP_THK, _imp_mm, extent_product

    a = extent_product("What is the area of the 1'-9\" thick slab-on-grade?", Answer(value="9'-0' x 9'-11'", unit=None,
                                                                                    confidence=0.3))
    assert a.value == 89.25 and a.unit == "ft2" and a.confidence == 0.3
    b = extent_product("How big is the genset foundation in plan, in m²?", Answer(value="6200 x 4272", unit=None,
                                                                                  confidence=0.3))
    assert b.value == 26.486 and b.unit == "m2"
    c = extent_product("How many pump pads?", Answer(value="7'-0\" x 7'-0\"", unit=None, confidence=0.3))
    assert c.value == "7'-0\" x 7'-0\""
    assert _imp_mm(_IMP_THK.search('1’-9" SLAB-ON-GRADE W/#5 @ 8" E.W.')) == 533
    assert _imp_mm(_IMP_THK.search('12" SLAB-ON-GRADE U.N.O.')) == 305
    assert _BAR.findall('W/#5 @ 12"') == ["#5"]


def test_exploded_dash_line_across_a_room_does_not_split_it(tmp_path):
    """D1 17:23 (EFA school sheets): a grid / centre line drawn as dozens of short one-segment pieces with small gaps
    was closed into a wall by the gap closing and sliced the Main Hall (59.6 of 180 m²)."""
    import pymupdf

    from stoa.evidence.linework import measure_room

    f = str(tmp_path / "dashed.pdf")
    _room_sheet(f)
    doc = pymupdf.open(f)
    pg = doc[0]
    x = 60.0
    while x < 440:  # a dash line at y = 140 through both rooms: 3 pt pieces, 1.5 pt gaps, each its own path
        sh = pg.new_shape()
        sh.draw_line((x, 140), (x + 3, 140))
        sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
        sh.commit()
        x += 4.5
    out = str(tmp_path / "dashed2.pdf")
    doc.save(out)
    r = measure_room(out, 0, (200, 173), 100)
    assert r is not None and abs(r["area_m2"] / 37.34 - 1) <= 0.03, r


def test_area_table_on_an_image_text_layer_and_its_table_box():
    """H 17:26 / 18:36: Stoa's AFCEC A-2 as PNG. Rows pair from image text (px units: wide label-value gaps, one-line
    'LABEL = N SF' reads split); a tile-cut layer (',095 SF') still locates the table's box for a crop re-read."""
    from types import SimpleNamespace as NS

    from stoa.evidence.areatable import _item_lines, _pair, _table_box

    items = [("WEST WING DORMITORY =", [100, 100, 500, 140]), ("6,095 SF", [1300, 100, 1460, 140]),
             ("WEST WING CIRCULATION =", [100, 160, 520, 200]), ("914 SF", [1340, 160, 1460, 200]),
             ("TOTAL WEST WING = 7,009 SF", [100, 220, 1460, 260]), ("UTILITY=", [100, 280, 260, 320]),
             ("328 SF", [1340, 280, 1460, 320])]
    rs = _pair(_item_lines(items))
    got = {r[0]: r[1] for r in rs}
    assert got == {"WEST WING DORMITORY": 6095, "WEST WING CIRCULATION": 914, "TOTAL WEST WING": 7009,
                   "UTILITY": 328}
    cut = [("WEST WING D", [100, 100, 300, 140]), (",095 SF", [1320, 100, 1460, 140]), ("914 SF", [1340, 160, 1460, 200]),
           (",009 SF", [1320, 220, 1460, 260]), ("TOTAL WEST", [100, 220, 300, 260]), ("Bedroom", [3000, 900, 3200, 940])]
    tl = NS(size=(6000, 4000), items=[NS(text=t, box=b) for t, b in cut])
    assert _pair(_item_lines(cut)) == ()
    x0, y0, x1, y1 = _table_box(tl)
    assert x0 < 100 and x1 > 1460 and y0 < 100 and y1 > 260 and x1 < 3000


def test_measure_room_anchored_ignores_free_standing_court_lines(tmp_path):
    """X1 18:15 / captain 18:21 (2): on a solid-wall sheet the label of a sports hall sits inside court markings that
    touch no wall; the room is the hall (20 x 15 m at 1:200), not the court cell around the label. A thin partition
    that touches a wall still splits rooms."""
    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "hall.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    u = 1 / (200 * L.PT_TO_M_AT_1)  # pt per metre at 1:200
    x0, y0, x1, y1 = 100, 100, 100 + 20 * u, 100 + 15 * u
    t = 0.3 * u
    sh = pg.new_shape()
    for r in [(x0 - t, y0 - t, x1 + t, y0), (x0 - t, y1, x1 + 30 * u, y1 + t), (x0 - t, y0, x0, y1),
              (x1, y0 - t, x1 + t, y1), (x1 + 30 * u, y0 - t, x1 + 30 * u + t, y1 + t),
              (x1, y0 - t, x1 + 30 * u, y0)]:
        sh.draw_rect(pymupdf.Rect(*r))
    sh.finish(color=None, fill=(0, 0, 0))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sh.draw_rect(pymupdf.Rect(cx - 4 * u, cy - 3 * u, cx + 4 * u, cy + 3 * u))  # court: touches no wall
    sh.draw_line((cx, cy - 3 * u), (cx, cy + 3 * u))
    sh.draw_line((x1 + 15 * u, y0), (x1 + 15 * u, y1))  # partition in the next block: touches both walls
    sh.finish(color=(0, 0, 0), width=0.28)
    sh.commit()
    doc.save(f)
    m = L.measure_room(f, 0, (cx - 2 * u, cy), 200)
    assert m is not None and abs(m["area_m2"] - 300) < 0.03 * 300 and m["variant"].startswith("anch")
    right = L.measure_room(f, 0, (x1 + 7 * u, cy), 200)
    assert right is not None and abs(right["area_m2"] - 15 * 15) < 0.03 * 225


def test_lineplan_follows_a_leader_arrow_from_a_label_outside_its_room(tmp_path):
    """W5 18:26 (Stoa AFCEC A-101 'Break Room'): the room name is printed outside the room with a straight arrow (filled
    head) pointing into it; the room is measured at the arrow's tip, not around the label."""
    import pymupdf

    from stoa.evidence import linework as L
    from stoa.evidence.lineplan import _arrow_tip, _measure

    f = str(tmp_path / "arrow.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    u = 1 / (100 * L.PT_TO_M_AT_1)  # 1:100
    x0, y0, x1, y1 = 200, 100, 200 + 5 * u, 100 + 4 * u
    sh = pg.new_shape()
    sh.draw_rect(pymupdf.Rect(x0, y0, x1, y1))
    sh.draw_rect(pymupdf.Rect(100, 50, 700, 560))  # the view frame: the label's own region is closed but huge
    cx = (x0 + x1) / 2
    sh.draw_line((cx, y1 + 30), (cx, y1 - 40))  # leader: tail under-ish the label, tip inside the room
    sh.finish(color=(0, 0, 0), width=0.5)
    sh.draw_polyline([(cx - 3, y1 - 34), (cx + 3, y1 - 34), (cx, y1 - 42), (cx - 3, y1 - 34)])
    sh.finish(color=(0, 0, 0), fill=(0, 0, 0), width=0.1)
    sh.commit()
    pg.insert_text((cx - 20, y1 + 42), "Break Room", fontsize=7)
    doc.save(f)
    box = next(b for t, b in L.text_lines(f, 0) if "Break" in t)
    tip = _arrow_tip(f, 0, tuple(box))
    assert tip is not None and x0 < tip[0] < x1 and y0 < tip[1] < y1
    m = _measure(f, 0, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), 100, tuple(box))
    assert m is not None and abs(m["area_m2"] - 20) < 0.06 * 20 and "leader arrow" in m.get("seed_note", "")


def test_measure_room_bridges_a_wide_opening_between_collinear_wall_stubs(tmp_path):
    """E1 c8 fallback (EFA 'Food bay', Nursery): a room opens into a labelled neighbour through a 2 m gap between two
    stubs of one solid wall, wider than any door gap; every normal reading swallows the neighbour's label, so the wall
    line is closed across the gap. Without the fallback: no outline."""
    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "bridge.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    u = 1 / (100 * L.PT_TO_M_AT_1)  # 1:100
    x0, y0, x1, y1, x2 = 100, 100, 100 + 6 * u, 100 + 5 * u, 100 + 10 * u
    t = 0.25 * u
    sh = pg.new_shape()
    for r in [(x0 - t, y0 - t, x2 + t, y0), (x0 - t, y1, x2 + t, y1 + t), (x0 - t, y0, x0, y1), (x2, y0, x2 + t, y1),
              (x1, y0, x1 + t, y0 + 1.5 * u), (x1, y1 - 1.5 * u, x1 + t, y1)]:  # middle wall: 2 m gap
        sh.draw_rect(pymupdf.Rect(*r))
        sh.finish(color=None, fill=(0, 0, 0))  # one fill path per wall piece, as the EFA sheets draw them
    sh.commit()
    pg.insert_text((x1 + 1.5 * u, (y0 + y1) / 2), "Lobby", fontsize=7)
    doc.save(f)
    L._page.cache_clear()
    m = L.measure_room(f, 0, (x0 + 2 * u, (y0 + y1) / 2), 100)
    assert m is not None and m["variant"].startswith("bridge@") and abs(m["area_m2"] - 30) < 0.04 * 30


def test_numbered_room_ranges_are_expanded():
    """X1 scan sc-077 'total floor area of English 1 to English 5' found no label (277.1 vs gold 275 once listed)."""
    from stoa.evidence.lineplan import expand_ranges as e

    assert e("What is the total floor area of English 1 to English 5?") == \
        "What is the total floor area of English 1, English 2, English 3, English 4 and English 5?"
    assert e("Group Room 1 to Group Room 3 combined area?") == "Group Room 1, Group Room 2 and Group Room 3 combined area?"
    assert e("How big are Classroom 3-6 together?").count("Classroom") == 4
    for q in ("What is the area between 3 to 5 m?", "levels 1 to 3", "grid lines A-1 to A-4", "How big is the English 1?"):
        assert e(q) == q


def test_traced_copy_gets_wall_fills_and_loses_its_dash_outlines(tmp_path, monkeypatch):
    """E1 cycle 9 (X1 19:20): a picture of a solid-wall sheet traced into ink OUTLINES has no fills (no solid-wall
    reading ran: Studio 47.79 vs 55.6 on the PDF) and its dashed grid lines are rows of small closed outlines that
    _drop_dashed never saw (they closed the Studio at 5.86 m instead of 6.76)."""
    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "pic.vec6.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    for x0, y0, x1, y1 in [(100, 100, 400, 103), (100, 297, 400, 300), (100, 100, 103, 300), (397, 100, 400, 300)]:
        sh.draw_polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])  # a 3 pt wall strip, outlined
    x = 105.0
    while x < 390:  # a dashed grid line across the room: 5 pt dashes, 2 pt gaps, each a closed outline
        sh.draw_polyline([(x, 199.6), (x + 5, 199.6), (x + 5, 200.4), (x, 200.4), (x, 199.6)])
        x += 7
    sh.finish(color=(0, 0, 0), width=0.24, closePath=False)
    sh.commit()
    pg.insert_text((200, 150), "Studio", fontsize=6, render_mode=3)
    doc.save(f)
    p = L._page(f, 0)  # default: dashes kept apart (rooms ignore them), no rebuilt fills (E1_TRACED_FILLS=0)
    assert len(p["strokes"]) == 4 and len(p["dashes"]) == 41 and not p["fills"]
    monkeypatch.setattr(L, "TRACED_FILLS", True)
    if hasattr(L._page, "cache_clear"):
        L._page.cache_clear()
    if hasattr(L._solid_walls, "cache_clear"):
        L._solid_walls.cache_clear()
    p = L._page(f, 0)
    assert len(p["fills"]) >= 1 and L._solid_walls(f, 0)
    if hasattr(L._page, "cache_clear"):
        L._page.cache_clear()
    if hasattr(L._solid_walls, "cache_clear"):
        L._solid_walls.cache_clear()


def test_area_table_on_a_damaged_image_layer_abstains_or_reads_the_crop(monkeypatch):
    """H 20:00 / QA 20:10 (Stoa's AFCEC PNG, a fresh VLM read): labels cut at the cell edge ('RMITORY', 'E CIRCULATION'),
    a neighbour tile's 'DORMITORY =' without its wing, title-block text to the RIGHT of a value whose own label was lost.
    Such a layer never answers on its own (it gave 'north wing dormitory' = the west wing's 6,095 @0.7 and 'total
    circulation' = one row 1,240 @0.7): the table crop is OCR'd; with no crop read the reader abstains. A question naming a
    wing never takes a row that names no wing on a table that names wings."""
    from types import SimpleNamespace as NS

    from stoa.evidence import areatable as A

    def it(t, b):
        return NS(text=t, box=b)

    dmg = [it("DORMITORY =", [4880, 1013, 5070, 1058]), it("6,095 SF", [5300, 1013, 5458, 1058]),
           it("TOTAL WEST WING =", [4576, 1181, 4900, 1227]), it("7,009 SF", [5300, 1181, 5457, 1227]),
           it("RMITORY =", [4880, 1564, 5040, 1609]), it("6,285 SF", [5300, 1564, 5457, 1609]),
           it("E CIRCULATION =", [4880, 1894, 5100, 1939]), it("1,240 SF", [5300, 1894, 5457, 1939]),
           it("6,092 SF", [5300, 1285, 5458, 1343]), it("FEBRUARY 2024", [5480, 1280, 5711, 1340])]
    tl = NS(size=(6000, 3882), items=dmg)
    monkeypatch.setattr(A, "_cached_layer", lambda p: tl)
    monkeypatch.setattr(A, "_crop_rows", lambda p, t, b: ())
    rs = A._pair(A._item_lines((x.text, x.box) for x in dmg), left_only=True)
    assert all(r[0] != "FEBRUARY 2024" for r in rs)  # title-block text right of a value is never its label
    assert A._suspect(rs, tl, A._table_box(tl))
    assert A.image_rows("plan.png") == ()
    assert A.answer("plan.png", "What is the dormitory area of the north wing in square feet?") is None
    good = [("WEST WING DORMITORY", 6095.0, "ft2", (0, 0, 9, 9)), ("WEST WING CIRCULATION", 914.0, "ft2", (0, 10, 9, 19)),
            ("TOTAL WEST WING", 7009.0, "ft2", (0, 20, 9, 29)), ("NORTH WING CIRCULATION", 1403.0, "ft2", (0, 30, 9, 39)),
            ("DORMITORY", 6285.0, "ft2", (0, 40, 9, 49))]
    monkeypatch.setattr(A, "_crop_rows", lambda p, t, b: tuple(good))
    assert A.image_rows("plan.png") == tuple(good)  # the crop's rows (no word test on the crop read)
    assert A.answer("plan.png", "What is the dormitory area of the north wing in square feet?") is None
    assert A.answer("plan.png", "What is the dormitory area of the west wing?").value == 6095
    nowing = [("DORMITORY", 6285.0, "ft2", (0, 0, 9, 9)), ("CIRCULATION", 1403.0, "ft2", (0, 10, 9, 19)),
              ("TOTAL", 7688.0, "ft2", (0, 20, 9, 29))]
    monkeypatch.setattr(A, "_crop_rows", lambda p, t, b: tuple(nowing))
    a = A.answer("plan.png", "What is the dormitory area of the north wing?")
    assert a is not None and a.value == 6285 and a.confidence <= 0.3 and "names no north" in a.assumptions[-1]
    from stoa.contract import Answer

    kept = A.prefer_printed_table("plan.png", "What is the dormitory area of the north wing?",
                                  Answer(value=6000, unit="ft2", confidence=0.4, solver="x"))
    assert kept.solver == "x" and "not used" in kept.trace[-1]


def test_dashed_room_separation_line_still_bounds_a_room(tmp_path, monkeypatch):
    """QA 20:00 (EFA 'PE Store', 50.19 ✓ at d23 -> abstained): a dashed line drawn wall to wall between two rooms is a
    ROOM SEPARATION, not a grid line: it is kept (both ends on wall ink, its middle on none). A dashed grid line that
    crosses the walls and ends outside the building is still dropped."""
    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "sep.pdf")
    _room_sheet(f)
    doc = pymupdf.open(f)
    pg = doc[0]

    def dashes(x0, x1, y):
        x = x0
        while x < x1:
            sh = pg.new_shape()
            sh.draw_line((x, y), (min(x + 3, x1), y))
            sh.finish(color=(0.5, 0.5, 0.5), width=0.25, closePath=False)
            sh.commit()
            x += 4.5

    dashes(60, 440, 140)  # grid line through both rooms, out past the walls
    dashes(100, 300, 190)  # separation inside Room A, wall to wall
    out = str(tmp_path / "sep2.pdf")
    doc.save(out)
    r = L.measure_room(out, 0, (200, 160), 100)
    k = 0.0352778  # m per pt at 1:100
    assert r is not None and abs(r["area_m2"] / (200 * 90 * k * k) - 1) <= 0.03, r
    monkeypatch.setattr(L, "KEEP_SEPARATIONS", False)
    L._page.cache_clear()
    L._dist.cache_clear() if hasattr(L._dist, "cache_clear") else None
    r2 = L.measure_room(out, 0, (200, 160), 100)
    assert r2 is not None and r2["area_m2"] > 30, r2


def test_straight_door_leaf_without_an_arc_closes_its_opening(tmp_path, monkeypatch):
    """QA 20:00 (EFA Food Room's bi-fold cupboard): a door drawn as a straight LEAF opened ~30° with no swing arc. Its
    closed position (the leaf turned onto the wall line at its hinge) closes the opening, as an arc's spokes do; without
    it the room runs on through the 1.06 m opening into the next room."""
    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "leaf.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    for a, b in [((100, 100), (400, 100)), ((100, 250), (400, 250)), ((100, 100), (100, 250)), ((400, 100), (400, 250)),
                 ((300, 100), (300, 150)), ((300, 180), (300, 250))]:
        sh.draw_line(a, b)
    sh.finish(color=(0, 0, 0), width=1.0)
    sh.draw_line((300, 150), (285, 150 + 30 * 0.8660254))  # the leaf: 30 pt (1.06 m at 1:100), 30° off the wall line
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.commit()
    doc.save(f)
    k = 0.0352778  # m per pt at 1:100
    r = L.measure_room(f, 0, (200, 175), 100)
    assert r is not None and abs(r["area_m2"] / (200 * 150 * k * k) - 1) <= 0.04, r
    monkeypatch.setattr(L, "DOOR_LEAVES", False)
    L._dist.cache_clear()
    r2 = L.measure_room(f, 0, (200, 175), 100)
    assert r2 is None or r2["area_m2"] > 45, r2


def test_area_table_sum_from_an_image_whose_totals_do_not_add_up_stays_low(monkeypatch):
    """X1 20:38 (AFCEC 300-dpi scan): the read lost the EAST wing's dormitory row, so 'total dormitory across all three
    wings' summed two rows (12,380 of 18,472) at 0.6 and replaced a right answer. With a TOTAL row that does not add up
    the sum stays <= 0.3 (a right 0.4 answer is kept); a one-row lookup <= 0.5."""
    from stoa.contract import Answer
    from stoa.evidence import areatable as A

    rows = (("WEST WING DORMITORY", 6095.0, "ft2", (0, 0, 9, 9)), ("WEST WING CIRCULATION", 914.0, "ft2", (0, 10, 9, 19)),
            ("TOTAL WEST WING", 7009.0, "ft2", (0, 20, 9, 29)), ("EAST WING CIRCULATION", 913.0, "ft2", (0, 30, 9, 39)),
            ("TOTAL EAST WING", 7005.0, "ft2", (0, 40, 9, 49)), ("NORTH WING DORMITORY", 6285.0, "ft2", (0, 50, 9, 59)),
            ("NORTH WING CIRCULATION", 1403.0, "ft2", (0, 60, 9, 69)), ("TOTAL NORTH WING", 7688.0, "ft2", (0, 70, 9, 79)))
    monkeypatch.setattr(A, "image_rows", lambda p: rows)
    q = "What is the total dormitory area on this floor across all three wings?"
    a = A.answer("scan.png", q)
    assert a is not None and a.value == 12380 and a.confidence <= 0.3
    kept = A.prefer_printed_table("scan.png", q, Answer(value=18472, unit="ft2", confidence=0.4, solver="raster"))
    assert kept.value == 18472 and kept.solver == "raster"
    one = A.answer("scan.png", "How many square feet of dormitory does the north wing have?")
    assert one.value == 6285 and 0.3 < one.confidence <= 0.5


def test_semicircle_door_swing_with_its_leaf_closes_the_doorway(tmp_path, monkeypatch):
    """H 20:37 (Stoa's DfE Multi-Purpose Hall, Ziying's skirting 50.4 m vs ours 53.83): a door drawn as a 180° swing
    (arc + the leaf back to the hinge, one path) was not taken for a door (30-100° only): the outline followed the arc and
    the opening was not deducted. With the leaf to the centre it is a door: the doorway is closed on the wall line."""
    import math

    import pymupdf

    from stoa.evidence import linework as L

    f = str(tmp_path / "semi.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    for a, b in [((100, 100), (400, 100)), ((100, 250), (400, 250)), ((100, 100), (100, 250)), ((400, 100), (400, 250)),
                 ((300, 100), (300, 150)), ((300, 180), (300, 250))]:
        sh.draw_line(a, b)
    sh.finish(color=(0, 0, 0), width=1.0)
    pts = [pymupdf.Point(300 + 30 * math.sin(math.radians(t)) * -1, 180 - 30 * math.cos(math.radians(t)))
           for t in range(0, 181, 12)]  # from the doorway tip (300, 150) round into Room A to (300, 210)
    sh.draw_polyline(pts + [pymupdf.Point(300, 180)])  # + the leaf back to the hinge
    sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
    sh.commit()
    doc.save(f)
    k = 0.0352778
    full = 200 * 150 * k * k
    r = L.measure_room(f, 0, (180, 175), 100)
    assert r is not None and abs(r["area_m2"] / full - 1) <= 0.025, (r and r["area_m2"], full)
    monkeypatch.setattr(L, "DOOR_SEMI", False)
    L._dist.cache_clear()
    r2 = L.measure_room(f, 0, (180, 175), 100)
    assert r2 is None or r2["area_m2"] < 0.975 * full or r2["area_m2"] > 1.2 * full, (r2 and r2["area_m2"], full)
