"""P1 upload normaliser: DWG / generic SVG / HEIC / multi-page PDF -> a file the solvers read, evidence mapped back."""

from __future__ import annotations

import pytest

pymupdf = pytest.importorskip("pymupdf")

from stoa.contract import Answer, Evidence  # noqa: E402
from stoa.formats import dwg, normalize  # noqa: E402


def _plan_pdf(path, pages=1):
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page(width=600, height=400)
        if i == pages - 1:  # the last page is the plan: lines + room labels
            for k in range(40):
                pg.draw_line((50 + 10 * k, 50), (50 + 10 * k, 350), width=0.5)
            pg.insert_text((100, 100), "Kitchen 2.02 12.05 m²", fontsize=10)
        else:
            pg.insert_text((72, 120), "COVER SHEET", fontsize=28)
    doc.save(path)
    return str(path)


def test_dwg_version_and_errors(tmp_path, monkeypatch):
    fake = tmp_path / "a.dwg"
    fake.write_bytes(b"AC1032" + b"\0" * 200)
    assert dwg.dwg_version(fake) == "2018"
    monkeypatch.setattr(dwg, "converter", lambda: None)
    with pytest.raises(dwg.DwgConverterMissing, match="save it as DXF"):
        dwg.to_dxf(fake)
    txt = tmp_path / "b.dwg"
    txt.write_text("not a dwg")
    with pytest.raises(dwg.DwgConvertError, match="not a DWG"):
        dwg.to_dxf(txt)


def test_dwg_upload_without_converter_keeps_file_and_says_why(tmp_path, monkeypatch):
    fake = tmp_path / "plan.dwg"
    fake.write_bytes(b"AC1027" + b"\0" * 200)
    monkeypatch.setattr(dwg, "converter", lambda: None)
    prep = normalize.prepare(str(fake), "How many doors?")
    assert prep.kind == "as_is" and prep.path == str(fake) and "DXF" in prep.error
    ans = normalize.restore(Answer.abstain("x", "no reader"), prep)
    assert any("DWG" in m for m in ans.missing)


def test_generic_svg_becomes_vector_pdf_and_evidence_maps_back(tmp_path):
    src = _plan_pdf(tmp_path / "p.pdf")
    svg = tmp_path / "up.svg"
    with pymupdf.open(src) as d:
        svg.write_text(d[0].get_svg_image(text_as_path=False).replace('width="600"', 'width="300mm"')
                       .replace('viewBox="0 0 600 400"', 'viewBox="10 20 600 400"'))
    assert not normalize.is_ifc_svg(svg)
    prep = normalize.prepare(str(svg), "What is the area of the kitchen?")
    assert prep.kind == "svg_pdf" and prep.path.endswith(".pdf")
    with pymupdf.open(prep.path) as d:
        assert "12.05" in d[0].get_text()
        width_pt = d[0].rect.width
    ox, oy, k = prep.svg_frame
    assert (ox, oy) == (10, 20) and k == pytest.approx(600 / width_pt)
    ans = Answer(value=12.05, unit="m2", confidence=0.9, solver="t",
                 evidence=[Evidence(file=prep.path, page=0, bbox=[10, 20, 30, 40], bbox_space="pdf_pt",
                                    polygon=[[10, 20], [30, 20], [30, 40]])])
    normalize.restore(ans, prep)
    ev = ans.evidence[0]
    assert ev.file.endswith("up.svg") and ev.bbox_space == "svg" and ev.page is None
    assert ev.bbox == pytest.approx([10 + 10 * k, 20 + 20 * k, 10 + 30 * k, 20 + 40 * k])
    assert ev.polygon[1] == pytest.approx([10 + 30 * k, 20 + 20 * k])
    assert ans.assumptions[0].startswith("SVG read as a vector drawing")


def test_ifc_svg_is_left_for_svg_ifc(tmp_path):
    s = tmp_path / "plan.svg"
    s.write_text('<svg xmlns="http://www.w3.org/2000/svg"><g class="IfcSpace" data-name="1"/></svg>')
    assert normalize.is_ifc_svg(s)
    assert normalize.prepare(str(s), "How many rooms?").kind == "as_is"


def test_multipage_pdf_answers_from_picked_page_and_points_back(tmp_path):
    src = _plan_pdf(tmp_path / "set.pdf", pages=3)
    prep = normalize.prepare(src, "What is the area of the kitchen?")
    assert prep.kind == "page" and prep.page == 2 and prep.pages == 3
    with pymupdf.open(prep.path) as d:
        assert d.page_count == 1 and "Kitchen" in d[0].get_text()
    ans = Answer(value=12.05, unit="m2", confidence=0.9, solver="t",
                 evidence=[Evidence(file=prep.path, page=0, bbox=[1, 2, 3, 4], bbox_space="pdf_pt")])
    normalize.restore(ans, prep)
    assert ans.evidence[0].file.endswith("set.pdf") and ans.evidence[0].page == 2
    assert ans.evidence[0].bbox == [1, 2, 3, 4] and "page 3" in ans.assumptions[0]


def test_single_page_and_images_unchanged(tmp_path):
    src = _plan_pdf(tmp_path / "one.pdf")
    prep = normalize.prepare(src, "q")
    assert prep.kind == "as_is" and prep.path == src and not prep.notes
    ans = Answer(value=1, unit="count", confidence=0.5, solver="t", assumptions=["a"])
    assert normalize.restore(ans, prep).assumptions == ["a"]


def test_heic_without_decoder_is_a_plain_message(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_heif(name, *a, **k):
        if name in ("pi_heif", "pillow_heif"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_heif)
    f = tmp_path / "IMG_0001.HEIC"
    f.write_bytes(b"\0\0\0\x18ftypheic" + b"\0" * 100)
    prep = normalize.prepare(str(f), "How many rooms?")
    assert prep.kind == "as_is" and "HEIC" in prep.error and "JPEG" in prep.error


def test_svg_glyph_position_lists_do_not_lose_text(tmp_path):
    """MuPDF drops text with per-glyph x lists under a translating transform (IFC sheet: 217 of 1,679 chars)."""
    s = tmp_path / "g.svg"
    s.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">'
                 '<text font-size="1.6" transform="matrix(2.834467 0 0 2.834467 -100 -100)">'
                 '<tspan y="99.02" x="86.4844 87.5516 88.4412 89.3308 89.8636 90.753208 91.64281">Bedroom</tspan>'
                 '</text></svg>')
    pdf, _ = normalize.svg_to_pdf(s)
    with pymupdf.open(pdf) as d:
        assert "Bedroom" in d[0].get_text()


def test_svg_ifc_tells_same_numbered_spaces_apart_by_printed_long_name(tmp_path):
    """Gymzaal sheet: 3 IfcSpaces named '15' (gang, …); the long name printed above the number picks one."""
    from stoa.solvers.svg_ifc import SvgIfc

    def space(guid, x0, y0, x1, y1):
        return (f'<g class="IfcSpace" data-name="15" data-guid="{guid}"><path d="M{x0} {y0}H{x1}V{y1}H{x0}Z"/></g>')

    def label(x, y, name):
        return f'<text x="{x}" y="{y - 2}">{name}</text><text x="{x}" y="{y}">15</text>'

    svg = tmp_path / "sheet.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297">'
                   + space("A", 0, 0, 40, 40) + space("B", 100, 0, 120, 30)  # 1600 vs 600 mm² on paper
                   + label(20, 20, "gang") + label(110, 15, "kleedkamer") + "</svg>")
    ans = SvgIfc().solve(str(svg), "What is the net floor area of the room 'kleedkamer (15)' on '00 Begane grond', in m²?")
    assert ans.value == pytest.approx(6.0) and ans.confidence == 0.85  # 600 paper mm² at 1:100 (no doors) = 6 m²
    ans = SvgIfc().solve(str(svg), "What is the net floor area of the room 'zaal (15)' on '00 Begane grond', in m²?")
    assert ans.value is None  # no space labelled 'zaal': still ambiguous, abstain


def test_scanned_pdf_page_is_read_as_image_and_evidence_lands_on_the_page(tmp_path):
    from PIL import Image

    png = tmp_path / "scan.png"
    Image.new("L", (1200, 800), 255).save(png)  # 1200 px over a 576 pt page = 150 dpi
    pdf = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=576, height=384)
    pg.insert_image(pg.rect, filename=str(png))
    doc.save(pdf)
    prep = normalize.prepare(str(pdf), "How many rooms?")
    assert prep.kind == "scan" and prep.path.endswith(".png")
    assert prep.scan_zoom == pytest.approx(1200 / 576)
    with Image.open(prep.path) as im:
        assert im.size == (1200, 800)
    ans = Answer(value=3, unit="count", confidence=0.5, solver="t",
                 evidence=[Evidence(file=prep.path, bbox=[120, 80, 240, 160], bbox_space="image_px")])
    normalize.restore(ans, prep)
    ev = ans.evidence[0]
    assert ev.file.endswith("scan.pdf") and ev.page == 0 and ev.bbox_space == "pdf_pt"
    assert ev.bbox == pytest.approx([57.6, 38.4, 115.2, 76.8]) and ans.assumptions[0].startswith("scanned PDF")
    assert normalize.scan_png(_plan_pdf(tmp_path / "vec.pdf")) is None  # a vector sheet is not a scan


def test_svg_ifc_holes_circles_and_printed_scale(tmp_path):
    """E1 13:33: holes are subtracted, columns drawn as <circle> count, a printed '1:200' beats the door estimate."""
    from stoa.solvers.svg_ifc import SvgIfc, read_svg, scale_of

    svg = tmp_path / "s.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297">'
                   '<g class="IfcSpace" data-name="1" data-guid="S1">'
                   '<path d="M0 0H100V100H0Z M40 40H60V60H40Z" fill-rule="evenodd"/></g>'  # 10000 - 400 mm²
                   + "".join(f'<g class="IfcColumn" data-guid="C{i}"><circle cx="{10 * i}" cy="200" r="2"/></g>'
                             for i in range(1, 6))
                   + '<text x="300" y="290">Scale 1:200 · cut at 1.20 m</text></svg>')
    d = read_svg(str(svg))
    mpu, how = scale_of(d)
    assert mpu == 0.2 and "printed" in how
    s = SvgIfc()
    assert s.solve(str(svg), "How many columns are shown on this floor plan?").value == 5
    a = s.solve(str(svg), "What is the net floor area of the room '1'?")
    assert a.value == pytest.approx(9600 * 0.2 ** 2)  # 384 m² (9,600 paper mm² at 1:200)


def test_svg_ifc_wall_length_from_outlines(tmp_path):
    """Two walls 10 m x 0.2 m at 1:100 (100 x 2 paper mm): 2 x (10 - 0.1) = 19.8 m, conf 0.5, centre lines."""
    from stoa.solvers.svg_ifc import SvgIfc, min_rect

    area, long, short, line = min_rect([(0, 0), (100, 0), (100, 2), (0, 2)])
    assert (long, short) == (100, 2) and area == 200
    assert line[0] == pytest.approx((0, 1)) and line[1] == pytest.approx((100, 1))
    svg = tmp_path / "w.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297">'
                   '<g class="cut IfcWall" data-guid="W1"><path d="M0 0H100V2H0Z"/></g>'
                   '<g class="cut IfcWall" data-guid="W2"><path d="M0 10V110H2V10Z"/></g>'
                   '<text x="300" y="290">Scale 1:100</text></svg>')
    a = SvgIfc().solve(str(svg), "What is the total length of all walls on 'Ground floor', in metres?")
    assert a.value == pytest.approx(19.8) and a.unit == "m" and a.confidence == 0.5
    assert len(a.evidence) == 2 and a.evidence[0].polyline and a.evidence[0].bbox_space == "svg"


def test_pick_page_finds_storey_named_by_a_number_in_the_sheet_title(tmp_path):
    """A set with sheets 'Floor plan: -1 / 0 / 1': a quoted '0' must pick the '0' sheet, not any page with a 0 on it."""
    from stoa.formats.pages import pick_page

    doc = pymupdf.open()
    for name in ("-1", "0", "1"):
        pg = doc.new_page(width=600, height=400)
        pg.insert_text((40, 40), f"House — Floor plan: {name}", fontsize=12)
        pg.insert_text((40, 380), "Scale 1:100 · cut at 1.20 m above storey level · IfcConvert 0.9.0", fontsize=8)
        pg.insert_text((200, 200), f"Room\n{name}.1", fontsize=8)
        for k in range(10 + 5 * int(name == "1")):  # page '1' carries the most drawing
            pg.draw_line((50 + 10 * k, 50), (50 + 10 * k, 350))
    f = tmp_path / "set.pdf"
    doc.save(f)
    assert pick_page(f, "What is the net floor area of the room 'Room (0.1)' on '0', in m²?")[0] == 1
    assert pick_page(f, "Ile drzwi pokazano na rzucie kondygnacji „-1”?")[0] == 0
    assert pick_page(f, "How many doors are shown on this drawing?")[0] == 2  # nothing named: most drawing


def test_pick_page_everyday_storey_words(tmp_path):
    from stoa.formats.pages import pick_page, sheet_storey

    assert sheet_storey("Smiley West — Grundriss: 1.OG\nMaßstab 1:100") == "1.OG"
    assert sheet_storey("A-101\nGROUND FLOOR PLAN\n") == "GROUND"
    doc = pymupdf.open()
    for name in ("KG", "EG", "1.OG", "2.OG"):
        pg = doc.new_page(width=600, height=400)
        pg.insert_text((40, 40), f"Smiley West — Grundriss: {name}", fontsize=12)
        for k in range(10 + 5 * int(name == "2.OG")):
            pg.draw_line((50 + 10 * k, 50), (50 + 10 * k, 350))
    f = tmp_path / "set.pdf"
    doc.save(f)
    assert pick_page(f, "How many rooms are on the ground floor?")[0] == 1
    assert pick_page(f, "How many doors are on the first floor?")[0] == 2
    assert pick_page(f, "What is the total area of the basement?")[0] == 0
    assert pick_page(f, "How many doors are shown?")[0] == 3  # no storey named: most drawing


def test_dxf_with_other_layer_names_is_read_by_role(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from stoa.formats import dxflayers

    doc = ezdxf.new()
    msp = doc.modelspace()
    for lay in ("A-AREA", "A-AREA-IDEN", "A-DOOR", "A-GLAZ", "Sanitär", "Wände", "Möbel"):
        doc.layers.add(lay)
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "A-AREA"})
    msp.add_text("Küche", dxfattribs={"layer": "A-AREA-IDEN", "insert": (1, 1)})
    msp.add_arc((0, 0), 0.9, 0, 90, dxfattribs={"layer": "A-DOOR"})
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "Möbel"})
    msp.add_lwpolyline([(2, 3), (3, 3), (3, 3.2), (2, 3.2)], close=True, dxfattribs={"layer": "A-GLAZ"})
    f = tmp_path / "plan.dxf"
    doc.saveas(f)
    roles = dxflayers.layer_roles(f)
    assert roles["A-AREA"] == "ROOM_OUTLINE" and roles["A-AREA-IDEN"] == "ROOM_TEXT" and roles["A-DOOR"] == "DOOR"
    assert roles["A-GLAZ"] == "WINDOW" and "Möbel" not in roles
    prep = normalize.prepare(str(f), "How many doors?")
    assert prep.kind == "dxf_roles" and dxflayers.has_roles(prep.path) and "A-DOOR → DOOR" in prep.notes[0]
    ans = Answer(value=1, unit="count", confidence=0.9, solver="t",
                 evidence=[Evidence(file=prep.path, bbox=[0, 0, 1, 1], bbox_space="dxf")])
    assert normalize.restore(ans, prep).evidence[0].file.endswith("plan.dxf")
    # no recognisable layer names: unchanged file, a 'missing' line (the upload solver caps the dxf answer)
    doc0 = ezdxf.new()
    doc0.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3)], close=True)
    f0 = tmp_path / "zero.dxf"
    doc0.saveas(f0)
    p0 = normalize.prepare(str(f0), "How many doors?")
    assert p0.kind == "as_is" and any("layer names" in m for m in p0.missing)


def test_dxf_doors_as_blocks_are_flagged(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")

    doc = ezdxf.new()
    blk = doc.blocks.new("DOOR900")
    blk.add_arc((0, 0), 0.9, 0, 90)
    blk.add_line((0, 0), (0.9, 0))
    msp = doc.modelspace()
    doc.layers.add("A-DOOR")
    doc.layers.add("A-AREA")
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "A-AREA"})
    for x in (0, 2, 4):
        msp.add_blockref("DOOR900", (x, 0), dxfattribs={"layer": "A-DOOR"})
    f = tmp_path / "blocks.dxf"
    doc.saveas(f)
    prep = normalize.prepare(str(f), "How many doors?")
    assert prep.kind == "dxf_roles" and any("3 on DOOR" in m for m in prep.missing)


def test_upload_never_raises_on_broken_files(tmp_path):
    from stoa.solvers.upload import Upload

    s = Upload()
    for name, data in (("broken.svg", b"<svg class=\"IfcSpace\"><g"), ("broken.pdf", b"%PDF-1.7 garbage"),
                       ("empty.dxf", b""), ("plan.dwg", b"AC1032" + b"\0" * 64)):
        f = tmp_path / name
        f.write_bytes(data)
        ans = s.solve(str(f), "How many doors are there?")  # must return, never raise
        assert ans is not None and (ans.value is None or ans.confidence <= 0.5)


def test_pick_page_scanned_set_takes_the_page_with_most_ink(tmp_path):
    from PIL import Image, ImageDraw

    from stoa.formats.pages import pick_page

    doc = pymupdf.open()
    for kind in ("cover", "plan", "notes"):
        im = Image.new("L", (900, 600), 255)
        d = ImageDraw.Draw(im)
        if kind == "plan":
            for k in range(0, 900, 30):
                d.line([(k, 0), (k, 600)], fill=0, width=2)
                d.line([(0, k), (900, k)], fill=0, width=2)
        else:
            d.text((50, 50), kind.upper() + " SHEET", fill=0)
        f = tmp_path / f"{kind}.png"
        im.save(f)
        pg = doc.new_page(width=432, height=288)
        pg.insert_image(pg.rect, filename=str(f))
    s = tmp_path / "scanset.pdf"
    doc.save(s)
    k, why = pick_page(s, "How many rooms are there?")
    assert k == 1 and "scanned set" in why


def _floor_dxf(path, names):
    """Two rooms with labels + two door leaves (5-vertex polylines), layers named by `names` (role -> layer)."""
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new()
    for lay in set(names.values()):
        if lay not in doc.layers:
            doc.layers.add(lay)
    msp = doc.modelspace()
    for x0, label in ((0, "Küche"), (5, "Zimmer")):
        msp.add_lwpolyline([(x0, 0), (x0 + 4, 0), (x0 + 4, 3), (x0, 3)], close=True,
                           dxfattribs={"layer": names["ROOM_OUTLINE"]})
        msp.add_text(label, dxfattribs={"layer": names["ROOM_TEXT"], "insert": (x0 + 1, 1.5)})
    for x0 in (1, 6):
        msp.add_lwpolyline([(x0, 0), (x0 + 0.9, 0), (x0 + 0.9, 0.05), (x0, 0.05), (x0, 0)],
                           dxfattribs={"layer": names["DOOR"]})
    doc.saveas(path)
    return str(path)


def test_dxf_reader_layer_guard_d32(tmp_path):
    """D32: the dxf reader itself (any route, not only `upload`) reads AIA-named DXFs through a renamed copy and
    answers layer-less DXFs at conf <= 0.2 with a 'missing' line instead of a confident 0."""
    from stoa.formats.dxflayers import NO_ROLES_MISSING
    from stoa.solvers.vector.dxf_solver import DxfSolver, _layers

    swiss = {"ROOM_OUTLINE": "ROOM_OUTLINE", "ROOM_TEXT": "ROOM_TEXT", "DOOR": "DOOR"}
    aia = {"ROOM_OUTLINE": "A-AREA", "ROOM_TEXT": "A-AREA-IDEN", "DOOR": "A-DOOR"}
    zero = {"ROOM_OUTLINE": "0", "ROOM_TEXT": "0", "DOOR": "0"}
    (tmp_path / "s").mkdir(), (tmp_path / "a").mkdir(), (tmp_path / "z").mkdir()
    fs = _floor_dxf(tmp_path / "s" / "plan.dxf", swiss)
    fa = _floor_dxf(tmp_path / "a" / "plan.dxf", aia)
    fz = _floor_dxf(tmp_path / "z" / "plan.dxf", zero)
    assert _layers(fs).status == "swiss" and _layers(fs).read == fs
    assert _layers(fa).status == "relayered" and _layers(fa).read.endswith("plan.roles.dxf")
    assert _layers(fz).status == "none"
    s = DxfSolver(llm=0, opcheck=0)
    q = "How many doors are on this floor plan?"
    a_s, a_a, a_z = s.solve(fs, q), s.solve(fa, q), s.solve(fz, q)
    assert a_s.value == a_a.value == 2 and a_s.confidence == a_a.confidence
    assert not any("layers read by their names" in x for x in a_s.assumptions)
    assert any("A-DOOR → DOOR" in x for x in a_a.assumptions)
    assert all(ev.file == fa for ev in a_a.evidence if ev.file and ev.file.endswith(".dxf"))
    assert a_z.value is None or a_z.confidence <= 0.2
    assert NO_ROLES_MISSING in a_z.missing
    # a PDF next to an AIA-named DXF: the dxf reader takes the twin once its layers rename to the reader's names
    pdf = tmp_path / "a" / "plan.pdf"
    _plan_pdf(pdf)
    assert s.can_handle(str(pdf), q)


def test_upload_reads_unreadable_vector_sheet_as_image(tmp_path, monkeypatch):
    """X1 14:22: a vector PDF no PDF reader can read (text as outlines) ended at the prior; upload re-asks on the
    rendered page, maps the image evidence back to points on the PDF page and caps the confidence at 0.5. Not on
    IFC-exported sheets (bim_plan), whose questions need the model."""
    from stoa.pipeline import Pipeline
    from stoa.solvers.upload import Upload

    pdf = tmp_path / "sheet.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=400, height=300)
    for i in range(30):
        pg.draw_line((10 + 10 * i, 10), (10 + 10 * i, 290))
    doc.save(pdf)
    kind = {"k": "other"}

    def fake(self, path, question):
        rec = f'recognised: {{"kind": "{kind["k"]}"}}'
        if path.endswith(".pdf"):
            return Answer(value="1", unit="count", confidence=0.29, solver="pipeline/prior",
                          trace=[rec, "pdf_vector: abstained (no printed scale)"])
        assert path.endswith(".png")
        return Answer(value=3, unit="count", confidence=0.75, solver="pipeline/vlm_lora", trace=[rec],
                      evidence=[Evidence(file=path, bbox=[0, 0, 100, 50], bbox_space="image_px")])

    monkeypatch.setattr(Pipeline, "solve", fake)
    a = Upload().solve(str(pdf), "How many bedrooms are there?")
    assert a.value == 3 and a.confidence == 0.5 and a.solver == "pipeline/vlm_lora"
    ev = a.evidence[0]
    assert ev.file.endswith("sheet.pdf") and ev.bbox_space == "pdf_pt" and ev.page == 0
    assert abs(ev.bbox[2] - 100 * 72 / 200) < 0.5  # no text layer -> 200 dpi pixels -> points
    assert any("read as an image" in x for x in a.assumptions)
    kind["k"] = "bim_plan"
    b = Upload().solve(str(pdf), "How many doors does the model have?")
    assert b.solver == "pipeline/prior"


def test_svg_read_as_image_maps_evidence_to_svg_units():
    """A generic SVG converted to PDF and then read as a picture: px -> PDF pt -> the SVG's own user coordinates."""
    prep = normalize.Prepared(original="/x/plan.svg", path="/x/plan.svg-v2.r200.png", kind="svg_pdf+render",
                              scan_zoom=2.0, svg_frame=(10.0, 20.0, 0.5))
    ans = Answer(value=1, unit="count", confidence=0.5, solver="t",
                 evidence=[Evidence(file=prep.path, bbox=[40, 60, 80, 100], bbox_space="image_px")])
    ev = normalize.restore(ans, prep).evidence[0]
    assert ev.bbox_space == "svg" and ev.file.endswith("plan.svg")
    assert ev.bbox == [10 + 20 * 0.5, 20 + 30 * 0.5, 10 + 40 * 0.5, 20 + 50 * 0.5]


def test_dwg_conversion_fixup_utf8_text_and_aligned_text(tmp_path):
    """LibreDWG artefacts (P1 14:58, Swiss DXF -> DWG -> DXF: apartment area 82.36 -> 0): UTF-8 bytes in an R2000 DXF
    read as cp1252 ('Ã ' for 'à'), and centred TEXT with align_point (0, 0) while insert is elsewhere."""
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new("R2000")
    t = doc.modelspace().add_text("Chambre à coucher", dxfattribs={"insert": (5, 7), "halign": 1, "valign": 2})
    t.dxf.align_point = (0, 0, 0)
    f = tmp_path / "x.dwg.dxf"
    doc.saveas(f)
    raw = f.read_bytes()
    assert "à".encode("cp1252") in raw
    f.write_bytes(raw.replace("à".encode("cp1252"), "à".encode("utf-8")))  # what LibreDWG wrote
    fixed = dwg.fix_converted(f)
    assert len(fixed) == 2
    e = next(iter(ezdxf.readfile(f).modelspace().query("TEXT")))
    assert e.dxf.text == "Chambre à coucher" and tuple(e.dxf.align_point)[:2] == (5, 7)
    assert dwg.fix_converted(f) == []  # a correct file is left alone


def test_dxf_door_and_window_blocks_are_counted(tmp_path):
    """Revit/AutoCAD exports draw doors/windows as blocks (INSERT) on A-DOOR / A-GLAZ; door tags (A-DOOR-IDEN) and a
    'Structure' layer are not doors. The reader adds one per block, at conf 0.6, with a box per block; via upload too."""
    ezdxf = pytest.importorskip("ezdxf")
    from stoa.solvers.upload import Upload
    from stoa.solvers.vector.dxf_solver import DxfSolver

    doc = ezdxf.new()
    blk = doc.blocks.new("Single-Flush - 36 x 84")
    blk.add_arc((0, 0), 0.9, 0, 90)
    blk.add_line((0, 0), (0.9, 0))
    tag = doc.blocks.new("Door Tag")
    tag.add_circle((0, 0), 0.2)
    msp = doc.modelspace()
    for lay in ("A-AREA", "A-AREA-IDEN", "A-DOOR", "A-DOOR-IDEN", "A-GLAZ", "Structure"):
        doc.layers.add(lay)
    msp.add_lwpolyline([(0, 0), (4, 0), (4, 3), (0, 3)], close=True, dxfattribs={"layer": "A-AREA"})
    msp.add_text("Office", dxfattribs={"layer": "A-AREA-IDEN", "insert": (1, 1)})
    for x in (0, 2, 4):
        msp.add_blockref("Single-Flush - 36 x 84", (x, 0), dxfattribs={"layer": "A-DOOR"})
        msp.add_blockref("Door Tag", (x, 1), dxfattribs={"layer": "A-DOOR-IDEN"})
    win = doc.blocks.new("Fixed - 36 x 48")  # a Revit window family: its name has no window word, its layer has
    win.add_line((0, 0), (0.9, 0))
    msp.add_blockref("Fixed - 36 x 48", (9, 9), dxfattribs={"layer": "A-GLAZ"})
    msp.add_line((0, 0), (5, 5), dxfattribs={"layer": "Structure"})
    f = tmp_path / "revit.dxf"
    doc.saveas(f)
    a = DxfSolver(llm=0, opcheck=0).solve(str(f), "How many doors are on this floor plan?")
    assert a.value == 3 and a.confidence == 0.6 and len([e for e in a.evidence if "block" in (e.note or "")]) == 3
    b = Upload(llm=0).solve(str(f), "How many doors and windows are there?")
    assert b.value == 4 and b.confidence == 0.6
    assert not any("drawn as blocks" in m for m in b.missing)


def _room_png(path, dpi=200, size=(1654, 2339)):
    """A4 @200 dpi, 1:100: one closed 4 x 3 m room (walls as 3 px lines) with a door-sized gap closed by a line."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    ppm = dpi / 0.0254 / 100  # px per metre at 1:100
    x0, y0 = 400, 600
    x1, y1 = x0 + round(4 * ppm), y0 + round(3 * ppm)
    d.rectangle([x0, y0, x1, y1], outline="black", width=3)
    d.rectangle([x0 - 120, y0 - 120, x1 + 120, y1 + 120], outline="black", width=3)  # a second ring (outer wall)
    im.save(path, dpi=(dpi, dpi))
    return str(path), (x0, y0, x1, y1)


def test_vectorize_paper_check(tmp_path):
    from PIL import Image

    from stoa.formats.vectorize import paper

    a4 = tmp_path / "a4.png"
    Image.new("RGB", (1654, 2339), "white").save(a4, dpi=(200, 200))
    assert paper(str(a4)) == (pytest.approx(200, abs=0.1), "A4")
    shot = tmp_path / "shot.png"  # a screenshot: 96 dpi, 1920 x 1080 is no paper size
    Image.new("RGB", (1920, 1080), "white").save(shot, dpi=(96, 96))
    assert paper(str(shot)) is None
    bare = tmp_path / "bare.png"  # a photo / export without DPI metadata
    Image.new("RGB", (1654, 2339), "white").save(bare)
    assert paper(str(bare)) is None


def test_vectorize_room_measured_by_lineplan(tmp_path, monkeypatch):
    """PNG of a line drawing -> vector PDF + OCR words as invisible text -> E1's lineplan measures the room at the
    printed scale on the copy (a 4 x 3 m room drawn with 3 px walls)."""
    from stoa.formats import vectorize as V
    from stoa.solvers.raster import textlayer as TL

    png, (x0, y0, x1, y1) = _room_png(tmp_path / "plan.png")
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    items = [TL.TextItem("OFFICE", [cx - 60, cy - 15, cx + 60, cy + 15]),
             TL.TextItem("SCALE 1:100", [100, 2200, 400, 2240])]
    monkeypatch.setattr(TL, "text_layer", lambda p, **kw: TL.TextLayer(path=p, size=(1654, 2339), items=items))
    info = V.to_vector_pdf(png, str(tmp_path / "plan.vec.pdf"))
    assert info["paper"] == "A4" and info["scale_n"] == 100 and info["words"] == 2
    with pymupdf.open(info["pdf"]) as doc:
        pg = doc[0]
        assert pg.rect.width == pytest.approx(1654 * 72 / 200, rel=1e-3)  # page = the paper sheet
        txt = pg.get_text()
        assert "OFFICE" in txt and "1:100" in txt  # invisible, but extractable
        assert len(pg.get_drawings()) >= 2
    lineplan = pytest.importorskip("stoa.evidence.lineplan")
    ans = lineplan.answer(info["pdf"], "How big is the office?")
    ppm = 200 / 0.0254 / 100
    # D40 (S0 16:31): lineplan measures to the MIDDLE of the room's own line (E1 0b17da8). PIL draws the 3 px walls
    # inward, so the line middle is 1 px inside the outer edge: (x1 - x0 - 2) x (y1 - y0 - 2). Branches without E1
    # 0b17da8 still measure to the wall faces ((x1 - x0 - 5) x (y1 - y0 - 5) = 11.55 m2); the copy is right either way.
    middle = (x1 - x0 - 2) * (y1 - y0 - 2) / ppm ** 2
    faces = (x1 - x0 - 5) * (y1 - y0 - 5) / ppm ** 2
    assert ans is not None
    assert ans.value == pytest.approx(middle, rel=0.02) or ans.value == pytest.approx(faces, rel=0.02)


def test_upload_vector_fallback_only_on_weak_answers():
    from stoa.solvers.upload import _intent, _weak

    a = Answer(value="Not determinable", unit=None, confidence=0.3, solver="pipeline/raster",
               trace=["recognised: {}", "intent: area room [drawing] (lang en)"])
    assert _weak(a) and _intent(a) == "area"
    b = Answer(value=12.5, unit="m2", confidence=0.75, solver="pipeline/raster", trace=["intent: area room"])
    assert not _weak(b)
    c = Answer(value=4.5, unit="m", confidence=0.3, solver="pipeline/raster", trace=["intent: length x"])
    assert _weak(c) and _intent(c) == "length"


def test_real_cad_zero_is_not_a_measurement(monkeypatch, tmp_path):
    """Revit DWGs (P1 15:50): 'total floor area' 0 m2 @0.96 from a DXF with no closed room outlines -> no value + why;
    a 0 count -> kept at <= 0.2; Swiss-schema DXFs untouched."""
    from types import SimpleNamespace

    from stoa.solvers import upload as U
    from stoa.solvers.vector import dxf_solver as D

    prep = SimpleNamespace(path=str(tmp_path / "a.dwg.dxf"), original=str(tmp_path / "a.dwg"))
    monkeypatch.setattr(D, "_layers", lambda p: SimpleNamespace(status="relayered"))
    a = U._real_cad_zero(Answer(value=0, unit="m2", confidence=0.96, solver="pipeline/dxf"), prep)
    assert a.value is None and a.confidence == 0 and any("closed room outlines" in m for m in a.missing)
    c = U._real_cad_zero(Answer(value=0, unit="count", confidence=0.99, solver="pipeline/dxf"), prep)
    assert c.value == 0 and c.confidence == 0.2
    n = U._real_cad_zero(Answer(value=7, unit="count", confidence=0.6, solver="pipeline/dxf"), prep)
    assert n.value == 7 and n.confidence == 0.6
    monkeypatch.setattr(D, "_layers", lambda p: SimpleNamespace(status="swiss"))
    s = U._real_cad_zero(Answer(value=0, unit="m2", confidence=0.96, solver="pipeline/dxf"), prep)
    assert s.value == 0 and s.confidence == 0.96


def test_pick_page_prefers_the_floor_plan_sheet(tmp_path):
    """X1 farm set (25-page US CAD set): 'How long is the house?' went to GENERAL NOTES ('house' x many), 'How many
    bedrooms?' to the cover. Floor-plan titled sheets win plan questions, elevations height questions; a cover with a
    sheet index is neither; 'bedrooms' finds 'BEDROOM'."""
    from stoa.formats.pages import _sheet_kind, pick_page

    doc = pymupdf.open()
    sheets = [
        ["COVER", "A0.1 PROJECT INFORMATION", "A0.2 GENERAL NOTES", "A1.2 1ST FLOOR PLAN", "A1.3 2ND FLOOR PLAN",
         "A2.1 FRONT ELEVATION"],
        ["GENERAL NOTES", "THE HOUSE SHALL BE BUILT TO CODE.", "EVERY HOUSE WALL IS FRAMED.", "HOUSE PORCH NOTES."],
        ["1ST FLOOR PLAN", "LIVING", "KITCHEN", "PORCH"],
        ["2ND FLOOR PLAN", "MAIN BEDROOM", "BEDROOM", "BATH"],
        ["FRONT ELEVATION (SOUTH)", "RIDGE", "32'-0\""],
    ]
    for lines in sheets:
        pg = doc.new_page(width=600, height=400)
        for i, ln in enumerate(lines):
            pg.insert_text((40, 40 + 18 * i), ln, fontsize=10)
        pg.draw_line((300, 100), (550, 100))
    path = tmp_path / "set.pdf"
    doc.save(path)
    with pymupdf.open(path) as d:
        kinds = [_sheet_kind(p.get_text()) for p in d]
    assert kinds == [None, None, "plan", "plan", "elev"]
    assert pick_page(path, "How many bedrooms does the house have?")[0] == 3
    assert pick_page(path, "How long is the house (outside walls)?")[0] in (2, 3)
    assert pick_page(path, "How tall is the house from the ground to the roof ridge?")[0] == 4


def test_page_render_carries_its_real_dpi(tmp_path):
    # O1 15:47: PyMuPDF tags every render 96 dpi; raster.scalebar.printed_bar needs the real one (an A4 page at 200
    # dpi = A4 paper) to turn a printed '1:100' into px/m. An old cache file with the wrong tag is rendered again.
    from PIL import Image

    pdf = pymupdf.open()
    pg = pdf.new_page(width=595.28, height=841.89)  # A4
    pg.draw_line((50, 50), (500, 50), width=1)
    pdf.save(tmp_path / "a4.pdf")
    png, zoom = normalize.render_png(str(tmp_path / "a4.pdf"), dpi=200)
    with Image.open(png) as im:
        dpi = im.info["dpi"][0]
        assert abs(dpi - 72 * zoom) < 0.01 and abs(dpi - 200) < 0.01
        from stoa.solvers.raster.scalebar import paper_of

        assert paper_of(im.size, dpi) == "A4"
    with Image.open(png) as im:  # simulate a cache file written by the old renderer (96 dpi tag)
        im.save(png, dpi=(96, 96))
    png2, _ = normalize.render_png(str(tmp_path / "a4.pdf"), dpi=200)
    with Image.open(png2) as im:
        assert abs(im.info["dpi"][0] - 200) < 0.01


def test_whole_house_count_sums_every_floor_plan_sheet(tmp_path, monkeypatch):
    """X1 farm set: 'How many toilets are there in the whole house?' was answered from one sheet (1 of 2). A whole-
    house count on a set with one floor-plan sheet per storey = the count on each, summed, evidence on each page;
    a storey-named or 'this sheet' question and stairs (drawn on every storey) keep the one-page answer."""
    from stoa.formats.pages import floor_plan_pages
    from stoa.pipeline import Pipeline
    from stoa.solvers.upload import Upload, whole_house_count

    doc = pymupdf.open()
    for lines in (["A0.1 PROJECT INFORMATION", "WC"], ["1ST FLOOR PLAN", "KITCHEN", "WC"],
                  ["2ND FLOOR PLAN", "BEDROOM", "WC", "BATH WC"], ["ROOF PLAN", "WC"]):
        pg = doc.new_page(width=600, height=400)
        for i, ln in enumerate(lines):
            pg.insert_text((40, 40 + 18 * i), ln, fontsize=10)
        pg.draw_line((300, 100), (550, 100))
    path = tmp_path / "set.pdf"
    doc.save(path)
    assert floor_plan_pages(path) == [(1, "1ST"), (2, "2ND")]

    def fake(self, p, q):  # counts 'WC' in the one-page copy's text, with a box on that page
        with pymupdf.open(p) as d:
            n = d[0].get_text().count("WC")
        return Answer(value=n, unit="count", confidence=0.7, solver="pipeline/label_count",
                      evidence=[Evidence(file=str(p), page=0, bbox=[1, 2, 3, 4], note=f"{n} WC")])

    monkeypatch.setattr(Pipeline, "solve", fake)
    a = Upload().solve(str(path), "How many toilets are there in the whole house?")
    assert a.value == 3 and a.confidence <= 0.6
    assert sorted(e.page for e in a.evidence) == [1, 2] and all(e.file.endswith("set.pdf") for e in a.evidence)
    assert any("every floor-plan sheet" in x for x in a.assumptions)
    one = Upload().solve(str(path), "How many toilets are on the 2nd floor?")
    assert one.value in (1, 2) and not any("every floor-plan sheet" in x for x in one.assumptions)
    assert not whole_house_count("How many risers does the stair in the house have?")
    assert not whole_house_count("How many doors are shown on this drawing?")
    assert whole_house_count("How many bedrooms does the house have?")


def test_stair_question_on_a_set_asks_g1s_stair_page_first(tmp_path, monkeypatch):
    # G1 16:35: 'How many risers lead up to the front porch?' -> the page whose printed stair note answers it
    import stoa.pipeline as P

    path = _plan_pdf(tmp_path / "set.pdf", pages=3)
    monkeypatch.setattr(P, "_stair_page", lambda src, q: (1, "page 2 prints the stair note '3'"), raising=False)
    prep = normalize.prepare(path, "How many risers lead up to the front porch?")
    assert prep.page == 1 and any("stair note" in n for n in prep.notes)
    monkeypatch.setattr(P, "_stair_page", lambda src, q: None, raising=False)
    assert normalize.prepare(path, "How big is the kitchen?").page == 2  # the usual pick (the plan page)


def test_dxf_blocks_are_doors_or_windows_by_their_name(tmp_path):
    """X1 dyimba (E1 17:18): window blocks 'A-Window-100cm' sit on layer A-Door with the doors, 2 sofas and a lavabo;
    a door block lies on a drawing layer. Role by block NAME first, else by layer; once the file names its doors,
    unnamed blocks on the door layer are not doors."""
    ezdxf = pytest.importorskip("ezdxf")
    from stoa.formats.dxflayers import block_counts
    from stoa.solvers.upload import Upload

    doc = ezdxf.new()
    for name in ("A-Door-80cm", "A-door-60cm", "A-Window-100cm", "A-Window-50cm", "Sofa", "lavabo"):
        b = doc.blocks.new(name)
        b.add_line((0, 0), (0.8, 0))
    msp = doc.modelspace()
    for lay in ("A-Wall", "A-Door", "A-Draw"):
        doc.layers.add(lay)
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 8), (0, 8)], close=True, dxfattribs={"layer": "A-Wall"})
    for i in range(4):
        msp.add_blockref("A-Door-80cm", (1 + 2 * i, 0), dxfattribs={"layer": "A-Door"})
    msp.add_blockref("A-door-60cm", (5, 5), dxfattribs={"layer": "A-Draw"})
    for i in range(3):
        msp.add_blockref("A-Window-100cm", (0, 1 + 2 * i), dxfattribs={"layer": "A-Door"})
    msp.add_blockref("A-Window-50cm", (10, 3), dxfattribs={"layer": "A-Door"})
    msp.add_blockref("Sofa", (4, 4), dxfattribs={"layer": "A-Door"})
    msp.add_blockref("lavabo", (6, 6), dxfattribs={"layer": "A-Door"})
    f = tmp_path / "house.dxf"
    doc.saveas(f)
    from stoa.formats.dxflayers import relayer

    copy, _ = relayer(f, tmp_path)
    assert block_counts(copy) == {"DOOR": 5, "WINDOW": 4}
    a = Upload(llm=0).solve(str(f), "How many windows are there?")
    assert a.value == 4 and len([e for e in a.evidence if "window block" in (e.note or "")]) == 4


def test_paper_from_pixels_needs_the_sheet_to_name_its_size():
    """Stoa's PNGs have no DPI tag; pixels alone are ambiguous by 2x (A1 @ 300 = A3 @ 600), so the sheet's printed
    size decides: S-100F 'ISO A1', DfE 'A2', M-101A 'ANSI D'; AFCEC (Tabloid @ 600) names none -> None."""
    from stoa.formats.paper import candidates, paper_from_pixels

    assert {n for n, _ in candidates((9934, 7017))} == {"A1", "A3"}
    dpi, name = paper_from_pixels((9934, 7017), ["SEE A1/S-513", "ISO A1", "A2"])
    assert name == "A1" and abs(dpi - 300) < 0.5
    assert paper_from_pixels((9934, 7017), ["SEE A1/S-513", "GRID A1"]) is None  # references, not a sheet size
    dpi, name = paper_from_pixels((14000, 9902), ["A2", "1:200"])
    assert name == "A2" and abs(dpi - 598.7) < 0.5
    assert paper_from_pixels((10200, 6600), ["ANSI D", "A1"])[1] == "ANSI D"
    assert paper_from_pixels((10200, 6600), ["1\" = 40'-0\""]) is None
    assert paper_from_pixels((1920, 1080), ["A4"]) is None and paper_from_pixels((4032, 3024), ["A3"]) is None


def test_render_of_a_vector_page_reads_the_pdfs_own_words_not_ocr(tmp_path, monkeypatch):
    """S0 16:48: Stoa's S-100F (A1, with a text layer) read as an image made PP-OCR run 10+ min per question. The
    render's text layer = the PDF's words scaled to render pixels; OCR and the VLM reading are never called."""
    from stoa.solvers.raster import textlayer as T

    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    pg.insert_text((100, 100), "PUMP ROOM", fontsize=12)
    pg.insert_text((100, 300), "SCALE: 1 : 50", fontsize=10)
    pg.draw_line((50, 50), (700, 50))
    doc.save(tmp_path / "s100.pdf")
    png, zoom = normalize.render_png(str(tmp_path / "s100.pdf"))
    assert normalize.seed_text_layer(str(tmp_path / "s100.pdf"), png, zoom) == 2

    def boom(*a, **k):
        raise AssertionError("OCR / VLM text reading must not run on a seeded render")

    monkeypatch.setattr(T, "_read", boom)
    monkeypatch.setattr(T, "ocr_suits", lambda p: True)
    import stoa.ocr as ocr

    monkeypatch.setattr(ocr, "text_layer", boom)
    tl = T.text_layer(png)
    words = [i.text for i in tl.items]
    assert words == ["PUMP ROOM", "SCALE: 1 : 50"]
    x0, y0, x1, y1 = tl.items[0].box
    assert abs(x0 - 100 * zoom) < 2 * zoom and y1 <= 100 * zoom + 4 * zoom  # boxes in render pixels
    assert [s["ratio"] for s in T.scales(tl)] == [50.0]
    # a page without a text layer (outlined text) is left to OCR
    doc2 = pymupdf.open()
    doc2.new_page().draw_line((50, 50), (300, 50))
    doc2.save(tmp_path / "outlined.pdf")
    png2, z2 = normalize.render_png(str(tmp_path / "outlined.pdf"))
    assert normalize.seed_text_layer(str(tmp_path / "outlined.pdf"), png2, z2) == 0


def test_shrink_image_keeps_a_clean_sheet_clean(tmp_path):
    """P1 17:15: the web upload's 6000-px LANCZOS resize turned 2 of Stoa's 4 clean CAD sheets into 'scans' for the
    clean-render gate (thin lines -> ramps of grey levels): text then went to CPU OCR, symbol readers switched off.
    shrink_image keeps a clean original clean (flat grey levels), leaves a scan's grey levels alone, scales the DPI
    with the pixels and does nothing to an image that is small enough."""
    import numpy as np
    from PIL import Image

    from stoa.formats.normalize import shrink_image
    from stoa.solvers.raster.windows import clean_render

    from PIL import ImageDraw

    rng = np.random.default_rng(1)
    img = Image.new("L", (8000, 5000), 255)
    d = ImageDraw.Draw(img)
    for _ in range(900):  # 1-px CAD lines at any angle (walls, hatching, leaders)
        x0, y0 = rng.integers(0, 8000), rng.integers(0, 5000)
        d.line([(x0, y0), (x0 + rng.integers(-900, 900), y0 + rng.integers(-900, 900))], fill=int(rng.choice([0, 90])))
    src = tmp_path / "sheet.png"
    img.save(src, dpi=(300, 300))
    assert clean_render(str(src))[0]
    plain = tmp_path / "plain.png"
    img.resize((6000, 3750), Image.LANCZOS).save(plain)
    assert not clean_render(str(plain))[0]  # what the plain resize does to it
    r = shrink_image(src, tmp_path / "sheet_small.png", 6000)
    assert r["clean"] and r["flat"] and r["size"] == (6000, 3750) and abs(r["factor"] - 0.75) < 1e-9
    assert clean_render(r["path"])[0]
    with Image.open(r["path"]) as im:
        assert abs(im.info["dpi"][0] - 225) < 0.5
    rng = np.random.default_rng(0)
    scan = rng.normal(200, 30, (5000, 8000)).clip(0, 255).astype(np.uint8)  # paper texture
    s = tmp_path / "scan.png"
    Image.fromarray(scan).save(s)
    r2 = shrink_image(s, tmp_path / "scan_small.png", 6000)
    assert r2["clean"] is False and not clean_render(r2["path"])[0]
    assert shrink_image(r["path"], tmp_path / "again.png", 6000)["factor"] == 1.0
    assert not (tmp_path / "again.png").exists()


def test_big_image_upload_is_read_from_a_smaller_copy_and_points_back(tmp_path, monkeypatch):
    """normalize.prepare reads an image above STOA_IMAGE_MAX_SIDE (default 6000 = the web upload's cap) from a cached
    smaller copy, so CLI / API / evals read what the UI reads; evidence comes back in the upload's own pixels."""
    from PIL import Image

    big = tmp_path / "sheet.png"
    Image.new("L", (9000, 3000), 255).save(big)
    prep = normalize.prepare(str(big), "How big is the kitchen?")
    assert prep.kind == "shrink" and abs(prep.img_scale - 6000 / 9000) < 1e-9
    with Image.open(prep.path) as im:
        assert im.size == (6000, 2000)
    assert normalize.prepare(str(big), "again").path == prep.path  # cached copy reused
    ans = Answer(value=12.0, unit="m2", confidence=0.5, solver="t",
                 evidence=[Evidence(file=prep.path, bbox=[600, 300, 1200, 900], bbox_space="image_px",
                                    polygon=[[600, 300], [1200, 300], [1200, 900]])])
    ans = normalize.restore(ans, prep)
    ev = ans.evidence[0]
    assert ev.file.endswith("sheet.png") and ev.bbox == pytest.approx([900, 450, 1800, 1350])
    assert ev.polygon[1] == pytest.approx([1800, 450])
    assert ans.assumptions[0].startswith("large image (9000×3000 px) read at 6000×2000 px")
    monkeypatch.setenv("STOA_IMAGE_MAX_SIDE", "0")
    assert normalize.prepare(str(big), "off").kind == "as_is"


def test_warm_reads_an_image_upload_before_the_first_question(tmp_path, monkeypatch):
    """stoa.formats.warm: the text layer of an uploaded image is read when the file arrives (the first question's
    37-81 s on Stoa's sheets), a big image via its smaller copy; it never raises."""
    from PIL import Image

    from stoa.formats import warm as W
    from stoa.solvers.raster import textlayer as T

    seen = []
    monkeypatch.setattr(T, "text_layer", lambda p, **kw: seen.append(p) or T.TextLayer(path=p, size=(1, 1), items=[]))
    monkeypatch.setattr(T, "is_clean", lambda p: True)
    big = tmp_path / "sheet.png"
    Image.new("L", (7000, 3000), 255).save(big)
    out = W.warm(str(big))
    assert out["error"] == "" and out["kind"] == "shrink" and seen == [out["solved_file"]]
    assert seen[0].endswith("sheet.s6000.png") and "text layer (0 texts)" in out["done"]
    bad = W.warm(str(tmp_path / "missing.png"))
    assert isinstance(bad, dict) and "seconds" in bad


def test_named_sheet_number_finds_its_sheet_in_a_set(tmp_path):
    """P1 17:35: 'on sheet M-101A' in a 156-page USACE set went to the page with the most drawing: the sheet pattern
    needed 2-4 digits and a word end ('M-101A', 'S-100F', 'A-2' never matched) and the set's index page lists every
    number too. The sheet's OWN number is printed big in its title block."""
    import pymupdf

    from stoa.formats.pages import pick_page, sheet_id

    d = pymupdf.open()
    for i, (big, small) in enumerate([("G-001", "SHEET INDEX\nM-100A PIPING PLAN\nM-101A PIPING PLAN\nA-2 FLOOR PLAN"),
                                      ("M-100A", "PIPING PLAN\nSEE SHEET M-101A\nCHECK VALVE"),
                                      ("M-101A", "PIPING PLAN\nSEE SHEET M-504\nCHECK VALVE"),
                                      ("A-2", "SECOND FLOOR PLAN\nCHECK VALVE")]):
        pg = d.new_page(width=1224, height=792)
        for j, line in enumerate(small.splitlines() + ["NOTES"] * 6):
            pg.insert_text((60, 80 + 14 * j), line, fontsize=9)
        pg.insert_text((1080, 740), big, fontsize=28)
        pg.draw_rect(pymupdf.Rect(100, 300, 900 - 100 * i, 700))
    f = tmp_path / "set.pdf"
    d.save(f)
    for q, want in [("How many check valves are there on sheet M-101A?", 2), ("On sheet M-100A: how many valves?", 1),
                    ("What is the area of the second floor (sheet A-2)?", 3)]:
        assert pick_page(f, q)[0] == want, q
    assert sheet_id(f, 2) == "M-101A" and sheet_id(f, 0) == "G-001"
    prep = normalize.prepare(str(f), "How many check valves are there on sheet M-101A?")
    assert prep.page == 2 and "answered from page 3 (sheet M-101A)" in prep.notes[0]


def test_scale_printed_for_another_paper_reads_the_page_enlarged(tmp_path):
    """D1 16:50 / X1 17:41: UK EFA sheets print '1:200 @ A1' on an A3 PDF page (true 1:400 on the file); every reader
    took the printed 1:200 (room areas x0.25). normalize.prepare reads such a page enlarged to the named paper, where
    the printed 1:N holds, and maps evidence back; a page whose paper agrees is left alone."""
    import pymupdf

    from stoa.formats.paper import scale_for_other_paper

    a3 = (420 / 25.4 * 72, 297 / 25.4 * 72)
    assert scale_for_other_paper("SCALE 1:200 @ A1", *a3)[:3] == pytest.approx((841 / 420, "A1", "A3"))
    assert scale_for_other_paper("1:100 at A3", *a3) is None and scale_for_other_paper("GRID A1  1:200", *a3) is None
    assert scale_for_other_paper("1:200 @ A1  1:50 @ A3", *a3) is None  # two papers named: not sure
    d = pymupdf.open()
    pg = d.new_page(width=a3[0], height=a3[1])
    pg.draw_rect(pymupdf.Rect(100, 100, 300, 200))
    pg.insert_text((900, 800), "Scale 1:200 @ A1", fontsize=10)
    f = tmp_path / "sheet.pdf"
    d.save(f)
    prep = normalize.prepare(str(f), "How big is the hall?")
    assert prep.kind == "paper" and prep.paper_f == pytest.approx(2.0, rel=0.01)
    with pymupdf.open(prep.path) as c:
        assert c[0].rect.width == pytest.approx(a3[0] * prep.paper_f, rel=1e-3)
        assert any(abs(r.rect.width - 200 * prep.paper_f) < 1 for r in [type("R", (), {"rect": dr["rect"]}) for dr in
                                                                         c[0].get_drawings()])
    assert "1:400 on this file" in prep.notes[0]
    ans = Answer(value=1.0, unit="m2", confidence=0.5, solver="t",
                 evidence=[Evidence(file=prep.path, page=0, bbox=[200, 200, 600, 400], bbox_space="pdf_pt")])
    ev = normalize.restore(ans, prep).evidence[0]
    assert ev.file.endswith("sheet.pdf") and ev.bbox == pytest.approx([v / prep.paper_f for v in (200, 200, 600, 400)])


def test_paper_of_a_shrunk_sheet_export_and_sheet_reference_removal(tmp_path):
    """E1 17:29: an untagged sheet export's pixel size gives its paper (paper_from_pixels) - also on the UI's 6000-px
    copy, which carries its original size; and once a named sheet's page is picked from a set, the readers get the
    question without 'sheet X' (it sent them to the sheet-table reader)."""
    from PIL import Image

    from stoa.formats.normalize import shrink_image, without_sheet
    from stoa.formats.paper import paper_of_image

    src = tmp_path / "a2.png"
    Image.new("L", (14000, 9902), 255).save(src)  # A2 at 598.7 dpi, no tag (Stoa's DfE sheet)
    r = shrink_image(src, tmp_path / "a2_small.png", 6000)
    dpi, sheet = paper_of_image(r["path"], ["ISO A2"])
    assert sheet == "A2" and dpi == pytest.approx(6000 / (594 / 25.4), rel=0.005)
    assert paper_of_image(r["path"], ["GRID A1"]) is None
    assert without_sheet("How many pumps? (sheet M-101A)", "M-101A") == "How many pumps?"
    assert without_sheet("On sheet S-100F: how big is the pad?", "S-100F") == "How big is the pad?"
    assert without_sheet("How many valves are on sheet M-101A?", "M-101A") == "How many valves are on the drawing?"
    assert without_sheet("How many valves on sheet M-101B?", "M-101A") == "How many valves on sheet M-101B?"


def test_shrunk_copy_is_made_once_under_concurrency_and_keeps_the_read_text(tmp_path, monkeypatch):
    """S0 17:42 (gate d21): 4 eval workers asked about one big sheet at once; one read the .s6000.png while another was
    still writing it ('cannot identify image file') and a right answer became the prior. The copy is made under a
    cross-process lock and written atomically; a text layer already read on the full-size image is carried over."""
    import concurrent.futures as cf
    import json

    from PIL import Image

    from stoa.solvers.raster import textlayer as T

    monkeypatch.setattr(T, "CACHE_DIR", tmp_path / "tl")
    big = tmp_path / "sheet.png"
    Image.new("L", (9000, 6000), 255).save(big)
    f0 = T._disk_key(str(big), 1100, "local")
    f0.parent.mkdir(parents=True)
    f0.write_text(json.dumps({"path": str(big), "size": [9000, 6000], "items": [{"text": "KITCHEN",
                                                                                   "box": [900, 600, 1800, 900]}]}))
    with cf.ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda _: normalize.shrunk_copy(big, 6000), range(8)))
    assert len({g[0] for g in got}) == 1
    with Image.open(got[0][0]) as im:
        im.load()
        assert im.size == (6000, 4000)
    d = json.loads(T._disk_key(got[0][0], 1100, "local").read_text())
    assert d["size"] == [6000, 4000] and d["items"][0]["box"] == [600, 400, 1200, 600]
    assert not list(tmp_path.glob(".*.tmp"))


def test_big_set_note_lists_the_sheets_with_the_questions_rare_words(tmp_path):
    """A question that names no sheet is ambiguous on a big standard-design set; the note lists the drawing sheets whose
    text carries the question's RARE words (by their own sheet numbers), so the visitor can re-ask naming one."""
    import pymupdf

    from stoa.formats.pages import related

    d = pymupdf.open()
    for i in range(10):
        pg = d.new_page(width=1224, height=792)
        words = "PIPING PLAN CHECK VALVE" + (" SUCTION HEADER 16 IN" if i == 6 else "")
        pg.insert_text((60, 80), words, fontsize=9)
        pg.insert_text((60, 100), "NOTES " * 5, fontsize=9)
        pg.insert_text((1080, 740), f"M-10{i}", fontsize=28)
        for j in range(i + 1):  # more drawing on later pages
            pg.draw_rect(pymupdf.Rect(100 + j, 300, 900, 700 - j))
    f = tmp_path / "set.pdf"
    d.save(f)
    assert related(f, "What size is the suction header?", exclude=9) == [(6, "M-106")]
    prep = normalize.prepare(str(f), "What size is the suction header?")
    assert "other sheets with the question's words: M-106 (p. 7)" in prep.notes[0] or prep.page == 6
