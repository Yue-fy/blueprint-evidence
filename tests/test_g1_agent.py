"""G1: Gemini plan-and-execute agent + Nemotron verifier, offline (no network: Gemini/Nemotron calls are faked)."""
from stoa.agent import gemini as G
from stoa.agent.verify import apply_verdict
from stoa.contract import Answer, Cost, Evidence


def _fake_ask(path, q):
    if "windows" in q and "model" in q:
        return Answer(value=297, unit="count", confidence=0.97, solver="ifc",
                      evidence=[Evidence(file="data/x.ifc", ifc_guid="g1", note="w")])
    if "windows" in q:
        return Answer(value=55, unit="count", confidence=0.88, solver="ifc",
                      evidence=[Evidence(file="data/x.pdf", page=0, bbox=[0, 0, 1, 1], bbox_space="pdf_pt")])
    return Answer(value=None, confidence=0.0, missing=["not found"])


class FakeGemini(G.GeminiAgent):
    def __init__(self, plan, final, **kw):
        super().__init__(ask_fn=_fake_ask, image=False, **kw)
        self.replies = [plan, final]

    def _call(self, msgs, schema, images, purpose, cost_box):
        cost_box[0] = cost_box[0].add(Cost(api_calls=1, llm_calls=1))
        return self.replies.pop(0)

    def _table(self, path):
        return "no structured table"


def test_plan_refs_calc_grounded():
    plan = {"understanding": "share", "steps": [
        {"id": "s1", "tool": "ask", "input": "How many windows are on '1.korrus'?"},
        {"id": "s2", "tool": "ask", "input": "How many windows are there in the model 'x'?"},
        {"id": "s3", "tool": "calc", "input": "{s1} / {s2} * 100"}]}
    final = {"answer": "s3", "source": "tools", "unit": "%", "reasoning": "s1/s2", "more_steps": []}
    a = FakeGemini(plan, final).solve("data/ifc_models/x.pdf", "What share of the windows is on 1.korrus?")
    assert abs(a.value - 18.5185) < 1e-3 and a.unit == "%"
    assert 0.7 < a.confidence < 0.9  # 0.9 * 0.88 * 0.97, grounded
    assert a.cost.api_calls == 2 and a.solver == "gemini_agent"
    assert any("calc(55 / 297 * 100)" in t for t in a.trace)


def test_guessed_number_is_not_grounded():
    plan = {"understanding": "x", "steps": [{"id": "s1", "tool": "calc", "input": "17 * 3"}]}
    final = {"answer": "s1", "source": "tools", "unit": "count", "reasoning": "guess", "more_steps": []}
    a = FakeGemini(plan, final).solve("data/ifc_models/x.pdf", "How many chairs?")
    assert a.value == 51 and a.confidence <= 0.25


def test_missing_ref_is_an_error_not_a_number():
    plan = {"understanding": "x", "steps": [
        {"id": "s1", "tool": "ask", "input": "How many chairs?"},
        {"id": "s2", "tool": "calc", "input": "{s1} * 2"}]}
    final = {"answer": "s2", "source": "tools", "unit": "count", "reasoning": "-", "more_steps": []}
    a = FakeGemini(plan, final).solve("data/ifc_models/x.pdf", "How many chair legs?")
    assert any("has no numeric value" in t for t in a.trace)
    assert a.confidence <= 0.25


def test_multi_part_answer():
    plan = {"understanding": "two parts", "steps": [
        {"id": "s1", "tool": "ask", "input": "How many windows are on '1.korrus'?"},
        {"id": "s2", "tool": "ask", "input": "How many windows are there in the model 'x'?"}]}
    final = {"answer": "s1 ; s2", "source": "tools", "unit": "count", "reasoning": "both", "more_steps": []}
    a = FakeGemini(plan, final).solve("data/ifc_models/x.pdf", "Windows on 1.korrus and in the model?")
    assert a.value == "55 ; 297" and 0.8 < a.confidence <= 0.9


def test_image_text_capped():
    plan = {"understanding": "title", "steps": []}
    final = {"answer": "Old Mill House", "source": "image", "unit": "", "reasoning": "title block", "more_steps": []}
    a = FakeGemini(plan, final).solve("data/habs/x.jpg", "What building is this?")
    assert a.value == "Old Mill House" and a.confidence == G.READ_CAP
    assert any("read by" in x for x in a.assumptions)


def test_verdict_moves_confidence_only():
    a = Answer(value=7, unit="count", confidence=0.4, trace=["t"])
    up = apply_verdict(a.model_copy(deep=True), {"verdict": "agree", "reason": "ok", "value_seen": "7", "cost": Cost()})
    down = apply_verdict(a.model_copy(deep=True), {"verdict": "disagree", "reason": "missed 2", "value_seen": "9",
                                                   "cost": Cost(api_calls=1)})
    same = apply_verdict(a.model_copy(deep=True), {"verdict": "unsure", "reason": "?", "value_seen": "", "cost": Cost()})
    boxed = apply_verdict(a.model_copy(deep=True), {"verdict": "disagree", "reason": "x", "value_seen": "9",
                                                    "boxes": 5, "cost": Cost()})
    assert up.value == down.value == same.value == boxed.value == 7
    assert up.confidence >= 0.4 > down.confidence and same.confidence == 0.4 and boxed.confidence == 0.4
    assert any("disagrees" in m for m in down.missing) and down.cost.api_calls == 1


def test_pipeline_flags_default_off(monkeypatch):
    monkeypatch.delenv("STOA_GEMINI_ESCALATE", raising=False)
    monkeypatch.delenv("STOA_VERIFY", raising=False)
    from stoa.pipeline import Pipeline

    p = Pipeline()
    assert not p.gemini and not p.verify
    assert Pipeline(gemini=1, verify="1").gemini


def test_relational_exists_not_composed():
    # W5 12:35: 'Is there a door between room 2.15 and room 2.10?' was composed into exists(all doors) -> yes @0.99
    from stoa.router.compose import decompose

    assert decompose("swiss_dwellings", "Is there a door between room 2.15 and room 2.10?") is None
    assert decompose("swiss_dwellings", "Is there a door from the kitchen to the corridor?") is None
    p = decompose("cubicasa5k", "Does this apartment have a sauna?")
    assert p is not None and p.op == "exists"


def test_object_typo_only_when_unparsed():
    from stoa.pipeline import normalise_object_typos as f

    assert f("How many bedoroms are there?") == "How many bedrooms are there?"
    assert f("What is the area of room 2.05 ('Korridor')?") == "What is the area of room 2.05 ('Korridor')?"
    assert f("How many halls are there?") == "How many halls are there?"


def test_multipart_detector_narrow():
    from stoa.router.multipart import is_multipart

    assert is_multipart("How many rooms labelled 'Zimmer' does apartment C have, and what is their total area?",
                        "swiss_dwellings")
    assert is_multipart("What are the net areas of apartment A and of apartment B?", "swiss_dwellings")
    assert not is_multipart("What is the total net area of apartment A and of apartment B?", "swiss_dwellings")
    assert not is_multipart("How many doors and windows are drawn on this floor plan in total?", "swiss_dwellings")
    assert not is_multipart("How many doors are on the ground floor, and how many on the top floor?", "ifc_models")


def test_plan_rooms_reads_ifc_sheet_areas():
    import pytest

    from stoa.data import abspath
    from stoa.solvers.plan_rooms import PlanRooms, read_plan

    rel = "data/ifc_models/bimspeed-warmond_ar_plan-03-1-level.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    plan = read_plan(rel)
    assert plan and plan["scale"] == 200 and len(plan["rooms"]) >= 50
    a = PlanRooms().solve(rel, "What is the net floor area of the room 'Kitchen (414B-1-001-007)' on '1. Level', in m²?")
    assert abs(a.value - 7.86) <= 0.02 * 7.86 and a.confidence >= 0.9 and a.evidence[0].bbox_space == "pdf_pt"
    assert PlanRooms().solve(rel, "What is the net floor area of apartment B?").value is None  # no apartments here


def test_modifier_typos_english_only():
    from stoa.pipeline import normalise_object_typos as f

    assert f("What is the area of the liivng room?") == "What is the area of the living room?"
    assert f("How many extreior walls are there?") == "How many exterior walls are there?"
    assert f("Quelle est la surface nette de l'appartement A (hors balcons)?").endswith("(hors balcons)?")
    assert f("What is the area of the 'Liivng' room?") == "What is the area of the 'Liivng' room?"


def test_singular_room_sum_reports_one():
    from stoa.contract import Answer, Evidence
    from stoa.pipeline import _singular_sum

    ev = [Evidence(file="m.ifc", ifc_guid=f"g{i}", note=f"Living {a} m²") for i, a in enumerate([25.27, 25.27, 36.2])]
    a = Answer(value=86.74, unit="m2", confidence=0.85, evidence=ev, trace=["[ifc] sum over 3 spaces"])
    _singular_sum("What is the area of the living room?", a)
    assert a.value == 25.27 and a.confidence == 0.5 and any("3 match" in m for m in a.missing)
    b = Answer(value=86.74, unit="m2", confidence=0.85, evidence=ev, trace=["[ifc] sum over 3 spaces"])
    _singular_sum("What is the total area of all living rooms?", b)
    assert b.value == 86.74 and b.confidence == 0.85


def test_multi_page_pdf_upload_solves_the_picked_page(tmp_path, monkeypatch):
    """P1 13:10: a 2-page upload (cover + Swiss plan) was answered from the cover (0/30); now from the plan page."""
    import fitz
    import pytest

    from stoa.data import abspath
    from stoa.pipeline import Pipeline, _pick_pdf_page

    rel = "data/swiss_dwellings/site57_bldg303_floor14020.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    monkeypatch.setenv("STOA_PAGE_DIR", str(tmp_path / "pages"))
    up = tmp_path / "upload" / "set.pdf"
    up.parent.mkdir()
    with fitz.open(abspath(rel)) as plan:
        doc = fitz.open()
        cover = doc.new_page(width=plan[0].rect.width, height=plan[0].rect.height)
        cover.insert_text((72, 72), "PROJECT COVER SHEET")
        doc.insert_pdf(plan)
        doc.save(up)
    q = "What is the net floor area of room 5.20 ('Cage d'escalier') in m²?"
    copy, k, n, _ = _pick_pdf_page(str(up), q)
    assert (k, n) == (1, 2) and not copy.startswith(str(up.parent))  # the copy never lands next to the upload
    a = Pipeline().solve(str(up), q)
    assert a.value is not None and abs(a.value - 13.98) <= 0.02 * 13.98
    assert all(e.page == 1 and e.file == str(up) for e in a.evidence if e.bbox_space == "pdf_pt")
    assert any(s.startswith("multi-page PDF (2 pages): answered from page 2") for s in a.assumptions)
    assert _pick_pdf_page(rel, q) is None  # dataset files keep their route


def test_length_kinds():
    from stoa.router.lengths import kind

    assert kind("How long is the corridor?") == "room_len"
    assert kind("How wide is the corridor in apartment B?") == "room_width"
    assert kind("What are the dimensions of room 2.01?") == "room_dims"
    assert kind("What is the perimeter of the largest room?") == "perimeter"
    assert kind("What are the outer dimensions of the building?") == "building_dims"
    assert kind("How long is the building?") == "building_len"
    assert kind("How long is the longest wall?") is None  # wall lengths are not room outlines
    assert kind("What is the area of the corridor?") is None
    assert kind("How many doors are in the corridor?") is None


def test_swiss_lengths_from_the_pdf_alone(tmp_path):
    """W5 13:13: 'How long is the corridor?' -> 40.41 m2 @0.6; now a length in m with the measured line."""
    import shutil

    import pytest

    from stoa.data import abspath
    from stoa.pipeline import Pipeline

    rel = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    up = tmp_path / "plan.pdf"
    shutil.copy(abspath(rel), up)
    p = Pipeline()
    a = p.solve(str(up), "How long is the corridor in apartment B?")
    assert a.unit == "m" and abs(a.value - 9.44) < 0.05 and a.evidence[0].polyline and a.solver == "pipeline/lengths"
    a = p.solve(str(up), "What are the dimensions of room 2.01?")
    assert a.value == "4.81 m × 3.28 m" and a.confidence >= 0.8
    a = p.solve(str(up), "How long is the building?")
    assert a.unit == "m" and abs(a.value - 40.71) < 0.1  # walls from the PDF = the DXF WALL layer (40.71 x 16.70)
    a = p.solve(str(up), "What is the total length of all walls?")  # room perimeters are not wall lengths
    assert a.confidence <= 0.3 and a.unit in ("m", None)


def test_ifc_lengths_from_footprints():
    """Building outer dimensions / longest wall / room dimensions on an IFC (before: fact-card text @0.3)."""
    import pytest

    from stoa.data import abspath
    from stoa.pipeline import Pipeline

    rel = "data/ifc_models/bimspeed-warmond_ar.ifc"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    p = Pipeline()
    a = p.solve(rel, "What are the outer dimensions of the building?")
    assert a.solver == "pipeline/lengths" and a.value.startswith("33.5") and a.evidence[0].ifc_guid
    a = p.solve(rel, "How long is the longest wall?")
    assert a.unit == "m" and abs(a.value - 33.21) < 0.01
    a = p.solve(rel, "What are the dimensions of the largest bedroom?")
    assert a.value == "4.07 m × 2.75 m"


def test_volume_cue_needs_a_word_boundary():
    from stoa.router.intent import infer_task

    assert infer_task("What is the net floor area of the room 'PROVIDER CUBICLES (1D16)' on 'First Floor', in m²?")[0] == "area"
    assert infer_task("What is the total net volume of all walls on 'KG', in m³?")[0] == "volume"


def test_recognize_reads_content_not_folder(tmp_path):
    import shutil

    import pytest

    from stoa.data import abspath
    from stoa.router.recognize import chip, recognize

    cases = {"data/ifc_models/bimspeed-warmond_ar_plan-03-1-level.pdf": ("bim_plan", "1. Level", "1:200"),
             "data/ifc_models/bimspeed-warmond_ar_elevation-south.pdf": ("bim_elevation", None, "1:200"),
             "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf": ("swiss_plan", None, "1:200"),
             "data/ifc_models/bimspeed-barlad_ro-arc-m3-e-pth-0000_plan-01-cta.svg": ("svg_bim", None, None)}
    for rel, (kind, storey, scale) in cases.items():
        if not abspath(rel).exists():
            pytest.skip("dataset not available")
        up = tmp_path / f"upload{abspath(rel).suffix}"
        shutil.copy(abspath(rel), up)  # neutral name and folder
        r = recognize(str(up))
        assert (r["kind"], r.get("storey"), r.get("scale")) == (kind, storey, scale), (rel, r)
        assert chip(r).startswith("recognised as: ")


def test_area_tags_on_a_revit_sheet(tmp_path):
    """E1 13:52: real Revit sheets alone; pdf_text gave 'Locker Room Men' as the largest (2 of 29 tags read)."""
    import shutil

    import pytest

    from stoa.data import abspath
    from stoa.pipeline import Pipeline

    rel = "data/opening_design/Sports_Complex/Models/2D/Sports Complex - Sheet - A-101 - First Floor Plan.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    up = tmp_path / "A101.pdf"
    shutil.copy(abspath(rel), up)
    p = Pipeline()
    a = p.solve(str(up), "What is the area of the clinic in m²?")
    assert a.solver == "pipeline/area_tags" and abs(a.value - 5941 * 0.09290304) < 0.05 and a.evidence[0].exact
    a = p.solve(str(up), "What is the smallest room?")
    assert str(a.value).startswith("Utility (66")
    a = p.solve(str(up), "How big is the reception?")
    assert a.value == 1185 and a.unit == "ft2"


def test_building_dims_from_dimension_strings(tmp_path):
    import shutil

    import pytest

    from stoa.data import abspath
    from stoa.pipeline import Pipeline

    rel = "data/opening_design/CTR/Most Current Set/Full size sheets/A101 - FLOOR PLANS - 1ST FLOOR.pdf"
    sp = "data/opening_design/Sports_Complex/Models/2D/Sports Complex - Sheet - A-101 - First Floor Plan.pdf"
    if not abspath(rel).exists() or not abspath(sp).exists():
        pytest.skip("dataset not available")
    shutil.copy(abspath(rel), tmp_path / "ctr.pdf")
    shutil.copy(abspath(sp), tmp_path / "sports.pdf")
    p = Pipeline()
    a = p.solve(str(tmp_path / "ctr.pdf"), "How long is the building in metres?")
    assert a.solver == "pipeline/lengths" and a.unit == "m" and abs(a.value - 52.72) < 0.05
    b = p.solve(str(tmp_path / "sports.pdf"), "What are the outer dimensions of the building?")
    assert b.solver != "pipeline/lengths"  # only partial 24'-34' strings next to a 27,042 SF room
    assert "fire rating" not in str(b.value)  # no stock sentence about other drawings on an upload


def test_user_scale_rescales_measured_values_only():
    from stoa.contract import Answer
    from stoa.pipeline import _apply_user_scale, _parse_scale

    assert _parse_scale("1:50") == 50 and _parse_scale(" 1 : 100 ") == 100 and _parse_scale("abc") is None
    a = Answer(value=8.0, unit="m2", confidence=0.9, solver="pipeline/plan_rooms",
               assumptions=["scale: 1:200 printed on the sheet; areas measured on the outlines"])
    assert _apply_user_scale(a, 100, None).value == 2.0
    b = Answer(value="4.00 m × 2.00 m", unit=None, confidence=0.9, solver="pipeline/lengths",
               assumptions=["scale: 1:100 read from the title block"])
    assert _apply_user_scale(b, 50, None).value == "2.00 m × 1.00 m"
    c = Answer(value=12.05, unit="m2", confidence=0.99, solver="pipeline/dxf",
               assumptions=["scale: measured in the DXF's own coordinates (metres), exact; the sheet is drawn at 1:200"])
    assert _apply_user_scale(c, 100, None).value == 12.05
    d = Answer(value=680.0, unit="ft2", confidence=0.9, solver="pipeline/area_tags", assumptions=[])
    assert _apply_user_scale(d, 50, {"scale": "1:100"}).value == 680.0  # printed on the sheet


def test_area_tags_named_scope_and_sums(tmp_path):
    """Gemini probe 14:1x: 'largest STUDIO' answered a 2BD @0.8; 'combined area of the laundry and the community
    room' summed every tag @0.6 (there is no laundry tag)."""
    import shutil

    import pytest

    from stoa.data import abspath
    from stoa.solvers.area_tags import AreaTags

    rel = "data/opening_design/CTR/Most Current Set/Full size sheets/A101 - FLOOR PLANS - 1ST FLOOR.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    up = tmp_path / "ctr.pdf"
    shutil.copy(abspath(rel), up)
    t = AreaTags()
    a = t.solve(str(up), "Which is the largest studio apartment and how big is it?")
    assert "STUDIO" in a.value and "tied with" in a.value and a.confidence <= 0.6
    assert t.solve(str(up), "What is the combined area of the laundry and the community room?").value is None
    assert t.solve(str(up), "What is the combined area of the office and the community room?").value == 599.0


def test_swiss_sheet_without_scale_reads_printed_room_areas(tmp_path):
    """Stoa 'scale unknown': with 'Massstab 1:200' removed, plan_rooms guessed 1:125 (room 2.01 10.08 m² @0.45) and
    area_tags summed every tag for 'apartment B' (320 m² @0.7); the printed label stack name / '2.01' / '15.61 m²'
    gives the exact value without any scale."""
    import pymupdf
    import pytest

    from stoa.data import abspath
    from stoa.solvers.area_tags import AreaTags

    rel = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"
    if not abspath(rel).exists():
        pytest.skip("dataset not available")
    d = pymupdf.open(abspath(rel))
    for needle in ("Massstab 1:200", "1:200 (A3)", "Massstab", "1:200"):
        for r in d[0].search_for(needle):
            d[0].add_redact_annot(r)
    d[0].apply_redactions(images=0, graphics=0)
    up = tmp_path / "plan.pdf"
    d.save(up)
    t = AreaTags()
    assert t.solve(str(up), "What is the area of room 2.01?").value == 15.61
    assert t.solve(str(up), "What is the total area of apartment B?").value is None


def test_parts_guard_names_the_missing_room():
    from stoa.contract import Answer, Evidence
    from stoa.pipeline import _parts_guard

    up = "/tmp/not-in-the-dataset/sheet.pdf"
    a = Answer(value=410.0, unit="ft2", confidence=0.55, solver="pipeline/pdf_text",
               evidence=[Evidence(file=up, note="text: COMMUNITY / ROOM / 115 - 410 SF")])
    out = _parts_guard("What is the combined area of the laundry and the community room?", a, up)
    assert out.confidence == 0.3 and "laundry" in out.missing[-1]
    swiss = Answer(value=477.98, unit="m2", confidence=0.96, solver="pipeline/pdf_vector",
                   evidence=[Evidence(file=up, note="room 3.01 'Zimmer': 9.78 m²")])
    q = ("What is the total net floor area of all numbered indoor rooms on this floor (excluding balconies, loggias, "
         "terraces and other outdoor spaces, shafts and voids)?")
    assert _parts_guard(q, swiss, up).confidence == 0.96


def test_parts_guard_keeps_one_row_per_named_room():
    from stoa.contract import Answer, Evidence
    from stoa.pipeline import _parts_guard

    up = "/tmp/not-in-the-dataset/A801.pdf"
    a = Answer(value=1181.0, unit="ft2", confidence=0.85, solver="pipeline/schedule",
               evidence=[Evidence(file=up, page=0, bbox=[1, 1, 2, 2], bbox_space="pdf_pt", note="schedule row: 611 SF"),
                         Evidence(file=up, page=0, bbox=[1, 3, 2, 4], bbox_space="pdf_pt", note="schedule row: 570 SF")])
    q = "On sheet A-801 of the Xcel Sports Complex drawings, what is the combined area of the women's and men's locker rooms?"
    assert _parts_guard(q, a, up).confidence == 0.85


def _label_pdf(path, labels):
    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    for i, t in enumerate(labels):
        pg.insert_text((60 + (i % 3) * 170, 80 + (i // 3) * 60), t, fontsize=9)
        pg.draw_rect(pymupdf.Rect(40 + (i % 3) * 170, 50 + (i // 3) * 60, 200 + (i % 3) * 170, 100 + (i // 3) * 60))
    doc.save(str(path))
    return str(path)


def test_label_count_counts_printed_room_labels(tmp_path):
    """X1 wild 14:22: house plans with 'BED ROOM 1..3' / 'WC' labels and no scale -> the prior said 1 (0/6)."""
    from stoa.solvers.label_count import LabelCount, asked_types

    p = _label_pdf(tmp_path / "house.pdf", ["BED ROOM 1", "BED ROOM 2", "bedroom3", "KITCHEN", "WC", "WC", "ensuit",
                                            "bathroom", "MEETING ROOM", "CONFERENCE", "Bedroom windows to be double"])
    s = LabelCount()
    a = s.solve(p, "How many bedrooms are there?")
    assert a.value == 3 and len(a.evidence) == 3 and a.evidence[0].bbox_space == "pdf_pt"
    assert s.solve(p, "How many toilets (WC) are there?").value == 2
    a = s.solve(p, "How many bathrooms are on this floor?")
    assert a.value == 1 and any("ensuit" in x for x in a.assumptions)  # a related label is named, not counted
    assert s.solve(p, "How many meeting rooms are on this level?").value == 1  # 'CONFERENCE' is another type
    assert s.solve(p, "How many garages are there?").value is None  # no label: abstain, never 0
    assert s.solve(p, "How many bedrooms are in apartment B?").value is None  # labels carry no membership
    assert "bedroom" in asked_types("How many bedrooms does this house have?")


def test_storey_roles_and_floor_to_floor():
    """X1 wild 14:22: 'upper floor' / 'upstairs' on Keller / Erdgeschoss / Obergeschoss / Dachgeschoss -> whole model."""
    from stoa.solvers.ifc.parse import storey_mention, storey_role

    names = ["Keller", "Erdgeschoss", "Obergeschoss", "Dachgeschoss"]
    assert storey_mention("What is the total area of all rooms on the upper floor?", names)[0] == 2
    assert storey_mention("the two bedrooms upstairs", names)[0] == 2
    assert storey_mention("How many rooms are in the basement?", names)[0] == 0
    assert storey_mention("What is the floor area of the attic?", names)[0] == 3
    assert storey_mention("on the ground floor", names)[0] == 1
    assert storey_role("rooms on the upper floor", ["EG", "1.OG", "2.OG"])[0] is None  # two upper floors: unresolved
    assert storey_mention("How many doors are above the basement?", names)[0] != 0  # a reference, not the storey


def test_length_cues_in_intent():
    from stoa.router.intent import infer_task

    assert infer_task("How wide is the platea at the stage end?")[0] == "length"
    assert infer_task("How much higher is the stage floor than the platea floor?")[0] == "length"
    assert infer_task("Wie breit ist das Bad 4.14?")[0] == "length"
    assert infer_task("What is the width of door 121.A?")[0] == "text"  # dataset schedule text answers stay text


def test_line_rooms_label_match():
    from stoa.solvers.line_rooms import _label

    lines = [("COMMS ROOM", (0, 0, 10, 5)), ("CONFERENCE", (20, 0, 30, 5)), ("ROOM", (20, 6, 30, 10)),
             ("MEETING ROOM", (40, 0, 50, 5)), ("MEETING ROOM", (60, 0, 70, 5))]
    assert _label("How big is the comms room?", lines)[0][0] == "COMMS ROOM"
    assert _label("How long is the conference room, wall to wall?", lines)[0][0] == "CONFERENCE"
    assert _label("How big is the meeting room?", lines)[0] is None  # two equal labels: which one?
    assert _label("How big is the hearing room?", lines)[0] is None


def test_gemini_vision_reading_maps_boxes(monkeypatch, tmp_path):
    """captain 14:39: readers abstained -> Gemini looks at the page; boxes (0-1000, y first) -> PDF points, capped."""
    import json

    from stoa import llm
    from stoa.agent import vision

    p = _label_pdf(tmp_path / "plan.pdf", ["1104"])

    class R:
        text = json.dumps({"answer": "1104", "unit": "cm", "confidence": 0.9, "how": "dimension string 1104 (cm)",
                           "evidence": [{"label": "1104", "box_2d": [0, 0, 500, 1000]}], "assumptions": []})
        prompt_tokens, completion_tokens = 1000, 100

    monkeypatch.setattr(llm, "chat", lambda *a, **k: R())
    a = vision.read(p, "How wide is the platea?")
    assert a.value == 11.04 and a.unit == "m" and a.confidence <= vision.VISION_CAP  # not Gemini's own 0.9
    assert a.evidence[0].bbox_space == "pdf_pt" and abs(a.evidence[0].bbox[2] - 595) < 1 and abs(a.evidence[0].bbox[3] - 421) < 1
    base = Answer(value=244.8, unit="m", confidence=0.0, solver="pipeline/prior")
    assert vision.wanted(base, "How wide is the platea?")
    b = vision.vision_step(p, "How wide is the platea?", base, why="x")
    assert b.solver == "pipeline/gemini_vision" and any("Gemini" in t for t in b.trace)
    # hybrid routing: a structured confident count is left alone; a LoRA count is checked; agreement raises confidence
    assert vision.wanted(Answer(value=3, unit="count", confidence=0.9, solver="pipeline/label_count"), "How many WCs?") is None
    assert vision.wanted(Answer(value=3, unit="count", confidence=0.75, solver="pipeline/vlm_lora"), "How many WCs?")
    R.text = json.dumps({"answer": "3", "unit": "count", "confidence": 0.2, "how": "three WC labels",
                         "evidence": [{"label": "WC", "box_2d": [0, 0, 10, 10]}] * 3})
    ours = Answer(value=3, unit="count", confidence=0.4, solver="pipeline/vlm_lora")
    c = vision.vision_step(p, "How many WCs are there?", ours, why="x")
    assert c.value == 3 and c.confidence >= vision.AGREE_CONF and any("agree" in x for x in c.assumptions)
    assert len(c.evidence) == 3


def test_answer_recognition_field_serialises():
    """D31: Answer.recognition is additive and optional (default None) and survives a JSON round trip."""
    a = Answer(value=1, unit="count", confidence=0.5)
    assert a.recognition is None
    b = Answer(value=1, unit="count", confidence=0.5, recognition={"kind": "bim_plan", "confidence": 0.9})
    c = Answer.model_validate_json(b.model_dump_json())
    assert c.recognition == {"kind": "bim_plan", "confidence": 0.9}


def test_hybrid_counts_only_on_uploads():
    """captain 15:01: hybrid routing is for uploads; a dataset CubiCasa count (the demo tour) is not sent to Gemini."""
    from stoa.agent import vision

    lora = Answer(value=3, unit="count", confidence=0.75, solver="pipeline/vlm_lora")
    assert vision.wanted(lora, "How many doors?", "data/cubicasa5k/colorful-4539.png") is None
    assert vision.wanted(lora, "How many doors?", "/tmp/upload/plan.png")
    prior = Answer(value=1, unit="count", confidence=0.2, solver="pipeline/prior")
    assert vision.wanted(prior, "How many doors?", "data/cubicasa5k/colorful-4539.png")  # nothing usable: still looks
