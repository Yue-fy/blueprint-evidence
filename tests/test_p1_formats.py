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
    # DR 21:57: the copy says which set and page it came from, wherever it lives (openings reads the DOOR SCHEDULE page)
    from stoa.formats import pages

    from pathlib import Path

    assert pages.copy_origin(prep.path) == (str(Path(src).resolve()), 2)
    assert pages.copy_origin(src) is None


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


def test_scan_with_a_scanners_invisible_ocr_layer_is_read_as_a_scan(tmp_path, monkeypatch):
    """X1 01:00: 'scan to searchable PDF' pages (one page-sized image + an INVISIBLE OCR layer, render mode 3) used to
    fail scan_png's text test and went 'page+render' with the scanner's (often junk) layer as their text."""
    from PIL import Image

    from stoa.formats import pages

    png = tmp_path / "scan.png"
    Image.new("L", (1200, 800), 255).save(png)

    def scan_pdf(name, render_mode, pages_n=1):
        doc = pymupdf.open()
        for _ in range(pages_n):
            pg = doc.new_page(width=576, height=384)
            pg.insert_image(pg.rect, filename=str(png))
            for i in range(30):
                pg.insert_text((40, 20 + 12 * i), "./ \u2022 \u00abSi,:, k4: lll ,,. ~ GROUND FLOOR", fontsize=8,
                               render_mode=render_mode)
        doc.save(tmp_path / name)
        return str(tmp_path / name)

    junk = scan_pdf("junk.pdf", 3)
    got = normalize.scan_png(junk)
    assert got is not None and got[0].endswith(".scan.png") and got[1] == pytest.approx(1200 / 576)
    assert normalize.prepare(junk, "How big is the kitchen?").kind == "scan"
    assert normalize.scan_png(scan_pdf("visible.pdf", 0)) is None  # VISIBLE text over a picture: not judged a scan
    two = scan_pdf("set.pdf", 3, pages_n=2)
    sc = pages.page_scores(two)
    assert [(p["vector_bytes"], p["own_chars"]) for p in sc] == [(0, 0), (0, 0)] and pages.scanned_set(sc)
    monkeypatch.setenv("STOA_SCAN_OCR_LAYER", "0")
    pages._SCORES.clear()
    assert normalize.scan_png(junk) is None and not pages.scanned_set(pages.page_scores(two))
    assert normalize.ocr_layer_scan(pymupdf.open(_plan_pdf(tmp_path / "vec.pdf"))[0]) is False


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


def test_pick_page_scanned_set_takes_the_page_with_most_ink(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw

    from stoa.formats.pages import pick_page
    from stoa.solvers.raster import textlayer as T

    # the pages' images are read for their text first (P1 19:40): stubbed, no model server in tests
    monkeypatch.setattr(T, "text_layer", lambda path, *a, **k: T.TextLayer(path=str(path), size=(900, 600), items=[
        T.TextItem({".p1.": "COVER SHEET", ".p3.": "NOTES SHEET"}.get(next((m for m in (".p1.", ".p3.") if m in
                                                                            str(path)), ""), "1"), [0, 0, 1, 1])]))

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
    # W5 19:33: each sheet's reading is the SAME printed building total ('TOTAL = 144 DORMS' on every sheet): once

    def fake_total(self, p, q):
        return Answer(value=144, unit="count", confidence=0.85, solver="pipeline/stated_count",
                      trace=["stated_count: the printed total row 'TOTAL =': 'TOTAL = 144 DORMS' = 144"])

    monkeypatch.setattr(Pipeline, "solve", fake_total)
    b = Upload().solve(str(path), "How many dorm rooms are there in the whole building?")
    assert b.value == 144 and b.confidence == 0.85
    assert "144 on the 1ST plan (page 2) + 144 on the 2ND plan (page 3)" in b.trace[0] and "not their sum" in b.trace[0]


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
    assert seen[0].endswith("sheet.s6000.png") and any(d.startswith("text layer (0 texts") for d in out["done"])
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


def test_paper_of_a_shrunk_copy_without_its_png_tag(tmp_path):
    """Gate d22: every cached 6000-px copy made before the PNG tag existed (Stoa's DfE copy, 17:35) lost its original
    size, so its paper was never found and the sheet was never traced. The shrunk_copy sidecar still has it; JPEG copies
    carry it as their comment. A sidecar whose factor does not map onto the file is ignored."""
    import json
    from pathlib import Path

    from PIL import Image

    from stoa.formats.normalize import shrink_image
    from stoa.formats.paper import paper_of_image

    want = 6000 / (594 / 25.4)
    old = tmp_path / "old.s6000.png"
    Image.new("L", (6000, 4244), 255).save(old)  # no tag: a copy cached by older code
    assert paper_of_image(str(old), ["ISO A2"]) is None
    Path(f"{old}.json").write_text(json.dumps({"src_size": [14000, 9902], "factor": 6000 / 14000, "note": ""}))
    dpi, sheet = paper_of_image(str(old), ["ISO A2"])
    assert sheet == "A2" and dpi == pytest.approx(want, rel=0.005)
    Path(f"{old}.json").write_text(json.dumps({"src_size": [10200, 6600], "factor": 6000 / 10200, "note": ""}))
    assert paper_of_image(str(old), ["ISO A2"]) is None  # 10200 x 6600 at 0.588 is not this file's 6000 x 4244
    src = tmp_path / "a2.jpg"
    Image.new("RGB", (14000, 9902), "white").save(src, quality=90)
    r = shrink_image(src, tmp_path / "a2.s6000.jpg", 6000)
    dpi, sheet = paper_of_image(r["path"], ["ISO A2"])
    assert sheet == "A2" and dpi == pytest.approx(want, rel=0.005)


def test_traced_copy_is_traced_once_for_concurrent_questions(tmp_path, monkeypatch):
    """P1 18:2x: 3 eval workers asking about one new sheet each traced it (~62 s each). One trace, the rest wait."""
    import concurrent.futures as cf
    import time
    from pathlib import Path

    from stoa.formats import vectorize as V

    png = tmp_path / "sheet.png"
    png.write_bytes(b"x")
    calls = []

    def fake(src, out, dpi=None, text=True):
        calls.append(src)
        time.sleep(0.3)
        d = pymupdf.open()
        d.new_page()
        d.set_metadata({"keywords": f"stoa-textsig:{V._current_sig(src)}"})
        d.save(out)
        return {}

    monkeypatch.setattr(V, "to_vector_pdf", fake)
    with cf.ThreadPoolExecutor(6) as ex:
        got = list(ex.map(lambda _: V.traced_copy(str(png), 200.0), range(6)))
    assert len(calls) == 1 and len(set(got)) == 1 and got[0].endswith(f"sheet.vec{V.VERSION}.pdf")


def test_traced_copy_made_at_another_dpi_is_traced_again(tmp_path, monkeypatch):
    """P1 19:45: the cached copy is keyed by name only; one traced at the pixel-size dpi must not be reused when the
    scale-bar source gives another (its page = pixels x 72 / dpi tells which dpi it was traced at)."""
    from pathlib import Path

    from PIL import Image

    from stoa.formats import vectorize as V

    png = tmp_path / "sheet.png"
    Image.new("L", (2000, 1000), 255).save(png)
    calls = []

    def fake(src, out, dpi=None, text=True):
        calls.append(dpi)
        d = pymupdf.open()
        d.new_page(width=2000 * 72 / dpi, height=1000 * 72 / dpi)
        d.set_metadata({"keywords": f"stoa-textsig:{V._current_sig(src)}"})
        d.save(out)
        return {}

    monkeypatch.setattr(V, "to_vector_pdf", fake)
    V.traced_copy(str(png), 200.0)
    V.traced_copy(str(png), 201.0)  # within 1 %: the same copy
    V.traced_copy(str(png), 150.0)
    assert calls == [200.0, 150.0]
    assert Path(V.traced_copy(str(png), 150.0)).exists() and calls == [200.0, 150.0]


def test_question_waits_for_the_uploads_running_warm(tmp_path, monkeypatch):
    """P1 18:3x: a question asked while the upload's warm pre-read runs used to read the sheet a second time in
    parallel; it now waits for warm (bounded) and then reads warm's caches. No warm running: no wait."""
    import threading
    import time

    from stoa.formats import warm as W

    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    png = tmp_path / "plan.png"
    png.write_bytes(b"x")
    assert W.wait_for_warm(png) == 0.0  # never warmed: no lock file, nothing waits
    started = threading.Event()

    def slow(path, later=None):
        started.set()
        time.sleep(1.0)
        return {"path": str(path)}

    monkeypatch.setattr(W, "_warm", slow)
    th = threading.Thread(target=W.warm, args=(str(png),))
    th.start()
    started.wait(5)
    waited = W.wait_for_warm(str(png))
    th.join()
    assert 0.5 <= waited <= 3.0
    assert W.wait_for_warm(png) < 0.5  # finished: the lock file stays, unlocked
    monkeypatch.setattr(W, "_warm", lambda p, later=None: time.sleep(2.0) or {})
    th = threading.Thread(target=W.warm, args=(str(png),))
    th.start()
    time.sleep(0.2)
    assert W.wait_for_warm(png, timeout=0.5) < 1.5  # bounded: a hung warm never blocks a question for long
    th.join()
    # QA 20:19: the UI warms upload/<id>/<name> but asks about a style-tagged COPY of the same bytes in another folder
    copy = tmp_path / "data" / "scan" / "plan.png"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(png.read_bytes())
    other = tmp_path / "other.png"
    other.write_bytes(b"y")
    monkeypatch.setattr(W, "_warm", lambda p, later=None: time.sleep(1.0) or {})
    th = threading.Thread(target=W.warm, args=(str(png),))
    th.start()
    time.sleep(0.2)
    assert W.wait_for_warm(copy) >= 0.5 and W.wait_for_warm(other) == 0.0
    th.join()


def test_warm_reads_an_outlined_text_pdf_page_before_the_first_question(tmp_path, monkeypatch):
    """Captain 18:21 (7): a one-page sheet whose text is outlines made the first question render + OCR it (1,148 s).
    warm does the same render and its text layer at upload; a sheet with real text is left alone (read in ms)."""
    from pathlib import Path

    from stoa.formats import warm as W
    from stoa.solvers.raster import textlayer as T

    seen = []
    monkeypatch.setattr(T, "text_layer", lambda p, *a, **k: seen.append(p) or T.TextLayer(path=p, size=(1, 1), items=[]))
    monkeypatch.setattr(T, "is_clean", lambda p: True)
    outl = tmp_path / "outlined.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=420 * 72 / 25.4, height=297 * 72 / 25.4)
    pg.draw_rect(pymupdf.Rect(50, 50, 500, 400), color=(0, 0, 0), width=1)
    d.save(outl)
    out = W.warm(str(outl))
    assert out["error"] == "" and any(x.startswith("page render + text layer") for x in out["done"])
    assert seen and seen[0].endswith(".png") and Path(seen[0]).exists()
    text = tmp_path / "text.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "GROUND FLOOR PLAN 1:100 KITCHEN 12.5 m2 LIVING ROOM 24.0 m2")
    d.save(text)
    seen.clear()
    out = W.warm(str(text))
    assert not seen and not any(x.startswith("page render") for x in out["done"])


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


def test_traced_copy_follows_the_text_layer(tmp_path, monkeypatch):
    """X1 20:43: a copy traced from an older (garbled OCR) text layer was reused after the layer changed; d27 lost 5 of
    9 DfE scan rows. The copy carries its text layer's signature; another layer (or none recorded: an older copy) =
    traced again."""
    from PIL import Image

    from stoa.formats import vectorize as V

    png = tmp_path / "sheet.png"
    Image.new("L", (2000, 1000), 255).save(png)
    layer = {"sig": "a" * 16}
    calls = []
    monkeypatch.setattr(V, "_current_sig", lambda p: layer["sig"])

    def fake(src, out, dpi=None, text=True):
        calls.append(layer["sig"])
        d = pymupdf.open()
        d.new_page(width=2000 * 72 / dpi, height=1000 * 72 / dpi)
        d.set_metadata({"keywords": f"stoa-textsig:{layer['sig']}"})
        d.save(out)
        return {}

    monkeypatch.setattr(V, "to_vector_pdf", fake)
    V.traced_copy(str(png), 200.0)
    V.traced_copy(str(png), 200.0)
    layer["sig"] = "b" * 16  # O1's gate / a newer OCR replaced the layer
    V.traced_copy(str(png), 200.0)
    assert calls == ["a" * 16, "b" * 16]
    old = tmp_path / "old.png"
    Image.new("L", (2000, 1000), 255).save(old)
    d = pymupdf.open()
    d.new_page(width=2000 * 72 / 200, height=1000 * 72 / 200)
    d.save(tmp_path / f"old.vec{V.VERSION}.pdf")  # traced before signatures existed
    V.traced_copy(str(old), 200.0)
    assert len(calls) == 3
    from stoa.formats.vectorize import text_sig
    from stoa.solvers.raster.textlayer import TextItem

    assert text_sig([TextItem(text="Hall", box=[1, 2, 3, 4], tile=0)]) != text_sig([TextItem(text="Ha11", box=[1, 2, 3, 4],
                                                                                              tile=0)])


def test_vector_pass_is_time_boxed_and_remembers_no_reading():
    """S0 18:42 (gate d24): slab questions on Stoa's S-100F PNG spent 60-167 s each on the traced copy's readers for no
    reading. The pass is time-boxed (STOA_VECTOR_BUDGET) and a (copy, intent) that gave nothing is skipped next time."""
    import time

    from stoa.solvers import upload as U

    ans, took = U._solve_within(lambda: 42, 1.0)
    assert ans == 42 and took < 1.0
    t0 = time.perf_counter()
    ans, took = U._solve_within(lambda: time.sleep(2.0) or 1, 0.2)
    assert ans is None and time.perf_counter() - t0 < 1.0
    # S0 19:18 (gate d25): running over is NOT remembered; the late answer is handed to late() in the thread, which
    # memos only 'no measured reading'
    got = []
    ans, _ = U._solve_within(lambda: time.sleep(0.4) or "late", 0.1, got.append)
    assert ans is None
    for _ in range(50):
        if got:
            break
        time.sleep(0.05)
    assert got == ["late"]
    a = Answer(value=1.0, unit="m2", confidence=0.3, trace=["recognised: {}", "intent: area slab [drawing] (lang en)"])
    assert U._intent_key(a) == "area slab"
    assert U._measured(Answer(value=151.79, solver="upload/plan_rooms")) and not U._measured(a)
    # E1 19:36: a dimension-confirmed slab area (>= 0.5) counts, a traced-only one (0.25) does not; DR 19:59: openings
    assert U._measured(Answer(value=61.69, unit="m2", confidence=0.5, solver="pipeline/slab_area"))
    assert not U._measured(Answer(value=194.0, unit="m2", confidence=0.25, solver="pipeline/slab_area"))
    assert U._measured(Answer(value=91, unit="count", confidence=0.4, solver="pipeline/openings"))
    assert U._intent_key(Answer(value=1.0, trace=["x"])) == ""
    # X1 20:38: the 'no reading' memo is per QUESTION (one 'area unknown' miss blocked other area questions on the sheet)
    assert U._question_key("How big is the Sports Hall?") == U._question_key("how big is the sports hall")
    assert U._question_key("How big is the Sports Hall?") != U._question_key("What is the area of the kitchen?")


def test_paper_copy_of_a_rotated_page_keeps_its_frame(tmp_path):
    """X1/E1 18:45-18:52: a '1:200 @ A1' A3 sheet with /Rotate 90 lost every vector reader on its paper copy (the copy
    was drawn as displayed: sideways text in its own frame). The copy keeps the unrotated frame x f and the /Rotate."""
    src = tmp_path / "rot.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=420 * 72 / 25.4, height=297 * 72 / 25.4)  # A3 landscape
    pg.insert_text((100, 100), "CLASSROOM 1")
    pg.insert_text((900, 800), "GROUND FLOOR PLAN 1:200 @ A1")
    pg.draw_line((50, 50), (300, 50))
    pg.set_rotation(90)
    d.save(src)
    path, f, note = normalize.paper_copy(str(src))
    assert f == pytest.approx(2.0, rel=0.01) and path.endswith(".paperA1.rot90.pdf") and "1:400" in note
    with pymupdf.open(str(src)) as a, pymupdf.open(path) as b:
        assert b[0].rotation == 90
        wa = [w for w in a[0].get_text("words") if w[4] == "CLASSROOM"][0]
        wb = [w for w in b[0].get_text("words") if w[4] == "CLASSROOM"][0]
        assert all(abs(x * f - y) < 1.0 for x, y in zip(wa[:4], wb[:4]))


def test_shrunk_copy_from_older_shrink_code_is_rebuilt(tmp_path):
    """H / X1 18:41-18:47: a cached 6000-px copy made by older shrink code (no version in its sidecar) was reused by
    every gate; it is now rebuilt once, and the rebuilt copy carries the original size."""
    import json
    from pathlib import Path

    from PIL import Image

    big = tmp_path / "sheet.png"
    Image.new("L", (9000, 6000), 255).save(big)
    path, f, _ = normalize.shrunk_copy(big, 6000)
    meta = Path(f"{path}.json")
    m = json.loads(meta.read_text())
    assert m["v"] == normalize.SHRINK_VERSION
    m.pop("v")
    meta.write_text(json.dumps(m))
    Image.new("L", (6000, 4000), 255).save(path)  # an old copy: no size tag
    path2, _, _ = normalize.shrunk_copy(big, 6000)
    assert path2 == path and json.loads(meta.read_text())["v"] == normalize.SHRINK_VERSION
    with Image.open(path2) as im:
        assert im.info.get("stoa_src_size") == "9000x6000"


def test_pick_page_finds_a_room_name_printed_on_one_page_of_a_set(tmp_path):
    """X1 / G1 18:35-18:56: 'What is the area of Science Lab 3?' on a GF / 1F / 2F set went to page 1 ('the most
    drawing') although only page 3 prints 'Science Lab 3'; 'Lab 30' is not 'Lab 3'."""
    from stoa.formats.pages import pick_page

    src = tmp_path / "set.pdf"
    d = pymupdf.open()
    for k, labels in enumerate((["Science Lab 30", "PE Store", "Hall"], ["Science Lab 31", "Library"],
                                ["Science Lab 3", "Science Lab 4", "Prep Room"])):
        pg = d.new_page(width=842, height=595)
        for i in range(60 if k == 0 else 10):  # page 1 carries the most drawing
            pg.draw_line((20 + i * 5, 20), (20 + i * 5, 500))
        for i, t in enumerate(labels):
            pg.insert_text((100, 100 + 40 * i), t)
    d.save(src)
    assert pick_page(src, "What is the area of Science Lab 3?")[0] == 2
    assert pick_page(src, "What's the net floor area of the PE Store?")[0] == 0


def test_paper_from_the_scale_bar_and_printed_ratio(tmp_path, monkeypatch):
    """X1 19:20: a scanned PDF's page image (no DPI tag, pixel size no standard paper) never got the vector readers and
    the trace said nothing. The scale bar's px per m x the printed 1:N gives the paper dpi; snapped to a standard sheet
    when the image is one."""
    from PIL import Image
    from stoa.formats import paper as _P
    if not hasattr(_P, "paper_of_bar"):  # S0 d26: P1 14e6c9e committed this test before paper.paper_of_bar (still WIP)
        pytest.skip("stoa.formats.paper.paper_of_bar not committed yet (P1 WIP)")

    from stoa.formats import paper as P
    from stoa.solvers.raster import scalebar as SB
    from stoa.solvers.raster import textlayer as T

    bar = SB.Bar(unit="m", px_per_unit=39.08, labels=[(0, 0, []), (20, 782, [])], box=[0, 0, 782, 30], residual=4.2,
                 source="layer")
    monkeypatch.setattr(SB, "bars", lambda path, layer=None, **k: [bar])
    a2 = tmp_path / "a2.png"
    Image.new("L", (4677, 3307), 255).save(a2)  # A2 at 200 dpi, untagged
    layer = T.TextLayer(path=str(a2), size=(4677, 3307), items=[T.TextItem("GROUND FLOOR PLAN 1:200", [0, 0, 10, 10])])
    dpi, sheet, how = P.paper_of_bar(str(a2), layer)
    assert sheet == "A2" and dpi == pytest.approx(200.0, rel=0.002) and "39.08 px per m" in how and "1:200" in how
    crop = tmp_path / "crop.png"
    Image.new("L", (3000, 2000), 255).save(crop)  # a crop: no paper -> the bar's own dpi
    dpi, sheet, _ = P.paper_of_bar(str(crop), layer)
    assert sheet == "custom" and dpi == pytest.approx(39.08 * 200 * 0.0254, rel=1e-6)
    layer.items = [T.TextItem("KITCHEN", [0, 0, 10, 10])]
    assert P.paper_of_bar(str(crop), layer) is None  # no printed 1:N


def test_cache_writes_are_atomic_per_thread(tmp_path):
    """X1 19:24 (gate d25 sc-110): _render wrote the scan PNG in place and a parallel question opened it half-written.
    Writes go to a per-process-and-thread temp name with the same suffix, then os.replace."""
    import threading

    names = []
    th = threading.Thread(target=lambda: names.append(normalize.tmp_path(tmp_path / "a.scan.png")))
    th.start()
    th.join()
    names.append(normalize.tmp_path(tmp_path / "a.scan.png"))
    assert names[0] != names[1] and all(n.suffix == ".png" and n.name.startswith(".a.scan.") for n in names)
    src = tmp_path / "p.pdf"
    d = pymupdf.open()
    d.new_page(width=200, height=100).insert_text((20, 50), "PLAN")
    d.save(src)
    dst = tmp_path / "p.scan.png"
    with pymupdf.open(src) as doc:
        normalize._render(doc[0], 2.0, dst, str(src))
    from PIL import Image

    with Image.open(dst) as im:
        assert im.size == (400, 200)
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_private_mode_blocks_every_hosted_model(monkeypatch):
    """Captain 18:53 / RL 19:21: private mode = no external call on ANY path (warm-up, scans, sets, the verifier).
    STOA_PRIVATE=1 makes stoa.llm refuse a hosted backend before any request or cached reply; our own servers
    (loopback, private network, Tailscale) stay allowed; STOA_EU_ONLY=1 keeps Verda (W5's EU option)."""
    from stoa import llm

    monkeypatch.setenv("STOA_PRIVATE", "1")
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    llm.check_private("local")
    for b in ("gemini", "nvidia", "verda"):
        with pytest.raises(llm.ExternalCallBlocked):
            llm.check_private(b)
    with pytest.raises(llm.ExternalCallBlocked):
        llm.chat("hello", backend="gemini")
    monkeypatch.setenv("STOA_MODE", "api")  # api mode reroutes local steps to Gemini: blocked too
    with pytest.raises(llm.LocalModelUnavailable):
        llm.chat("hello", backend="local")
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    llm.check_private("verda")
    assert llm._private_host("http://100.101.2.3:8001/v1") and not llm._private_host("https://example.com/v1")
    monkeypatch.setenv("STOA_PRIVATE", "0")
    llm.check_private("gemini")


def test_private_mode_request_flag_and_service_urls(monkeypatch):
    """Audit 19:51 risk 3 / T2: one request-level private state must block every hosted call. G1's Pipeline(private=1)
    sets a context var (stoa.agent.private) instead of the env: stoa.llm's guard honours it too. Non-LLM services (OCR,
    door detector) are reached by URL: check_private_url refuses a non-private host in private mode."""
    import contextvars
    import sys
    import types

    from stoa import llm

    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    fake = types.ModuleType("stoa.agent.private")
    fake._CTX = contextvars.ContextVar("stoa_private_test", default=False)
    monkeypatch.setitem(sys.modules, "stoa.agent.private", fake)
    import stoa.agent as agent_pkg

    monkeypatch.setattr(agent_pkg, "private", fake, raising=False)
    from stoa import netguard

    monkeypatch.setattr(netguard, "relay_for_url", lambda url: None, raising=False)  # not this machine's live tunnels
    llm.check_private("gemini")  # neither signal: allowed
    tok = fake._CTX.set(True)
    try:
        assert llm.private_mode()
        with pytest.raises(llm.ExternalCallBlocked):
            llm.check_private("nvidia")
        llm.check_private("local")
        llm.check_private_url("http://127.0.0.1:8110", "OCR server")
        with pytest.raises(llm.ExternalCallBlocked):
            llm.check_private_url("https://ocr.example.com", "OCR server")
    finally:
        fake._CTX.reset(tok)
    assert not llm.private_mode()
    llm.check_private_url("https://ocr.example.com")  # not private: allowed


def test_private_mode_refuses_a_local_port_that_forwards_outside(monkeypatch):
    """T2 00:26: 127.0.0.1:8145 is an ssh tunnel to V1's detector twin on another machine: in private mode the reader
    gets a clear refusal (netguard would refuse the socket anyway, reported as 'the detector service is down')."""
    from stoa import llm, netguard

    monkeypatch.setenv("STOA_PRIVATE", "1")
    monkeypatch.setattr(netguard, "relay_for_url", lambda url: "ssh tunnel to verda" if ":8145" in url else None,
                        raising=False)
    llm.check_private_url("http://127.0.0.1:8140", "door detector")
    with pytest.raises(llm.ExternalCallBlocked, match="forwards outside this server .ssh tunnel to verda"):
        llm.check_private_url("http://127.0.0.1:8145", "door detector")
    # H 01:45: EU mode (STOA_EU_ONLY=1) allows the relay ports the operator lists as our own EU instance, as netguard
    # does at the socket; strict private mode and unlisted ports are still refused
    monkeypatch.setattr(netguard, "eu_only", lambda: True, raising=False)
    monkeypatch.setattr(netguard, "_eu_relay_ports", lambda: {8145}, raising=False)
    llm.check_private_url("http://127.0.0.1:8145", "door detector")
    monkeypatch.setattr(netguard, "_eu_relay_ports", lambda: {8148}, raising=False)
    with pytest.raises(llm.ExternalCallBlocked):
        llm.check_private_url("http://127.0.0.1:8145", "door detector")
    monkeypatch.setattr(netguard, "eu_only", lambda: False, raising=False)
    monkeypatch.setattr(netguard, "_eu_relay_ports", lambda: {8145}, raising=False)
    with pytest.raises(llm.ExternalCallBlocked):
        llm.check_private_url("http://127.0.0.1:8145", "door detector")
    monkeypatch.setenv("STOA_PRIVATE_RELAY_NOTE", "0")  # kill switch: netguard alone decides
    llm.check_private_url("http://127.0.0.1:8145", "door detector")
    monkeypatch.delenv("STOA_PRIVATE_RELAY_NOTE")
    monkeypatch.delenv("STOA_PRIVATE")
    llm.check_private_url("http://127.0.0.1:8145", "door detector")  # not private: allowed


def test_warm_command_is_private(monkeypatch, tmp_path):
    """RL 19:21: the upload warm-up ran with the server's env and, with STOA_MODE=api, sent tiles to Gemini even with
    the visitor's Private switch on. `python -m stoa.formats.warm` now sets STOA_PRIVATE=1 for itself."""
    import os

    from stoa.formats import warm

    monkeypatch.setenv("STOA_PRIVATE", "0")  # recorded, so the '1' main() sets is undone after the test
    monkeypatch.delenv("STOA_WARM_HOSTED", raising=False)
    monkeypatch.setattr(warm, "warm", lambda p: {"path": p, "private": os.environ.get("STOA_PRIVATE")})
    warm.main([str(tmp_path / "x.png")])
    assert os.environ.get("STOA_PRIVATE") == "1"


def test_scanned_set_pages_are_picked_by_their_read_text(tmp_path, monkeypatch):
    """Captain 18:50: a SCANNED set (every page a picture) was picked by 'the most ink' only (P1's 3-page scanned EFA
    set: 9/24 on the right page). Each page's image text is read once, cached beside the set, and used for picking."""
    import io

    from PIL import Image, ImageDraw

    from stoa.formats import pages as PG
    from stoa.solvers.raster import textlayer as T

    d = pymupdf.open()
    for ink in (0.4, 0.1):  # page 1 has more ink: the old rule picks it for everything
        img = Image.new("L", (1200, 850), 255)
        ImageDraw.Draw(img).rectangle([50, 50, 50 + int(1100 * ink), 800], fill=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pg = d.new_page(width=842, height=595)
        pg.insert_image(pg.rect, stream=buf.getvalue())
    f = tmp_path / "set.pdf"
    d.save(f)
    said = {"p1": ["GROUND FLOOR PLAN", "SPORTS HALL"], "p2": ["FIRST FLOOR PLAN", "LIBRARY", "ENGLISH 1"]}

    def fake_layer(path, *a, **k):
        name = Path(path).name
        key = "p1" if ".p1." in name else "p2"
        return T.TextLayer(path=str(path), size=(1200, 850), items=[T.TextItem(t, [0, 0, 1, 1]) for t in said[key]])

    from pathlib import Path

    monkeypatch.setattr(T, "text_layer", fake_layer)
    assert PG.scanned_set(PG.page_scores(f))
    k, why = PG.pick_page(f, "How big is the Library?")
    assert k == 1 and "scanned pages: text read from their images" in why
    assert PG.cached_scan_texts(f) == {0: "GROUND FLOOR PLAN\nSPORTS HALL", 1: "FIRST FLOOR PLAN\nLIBRARY\nENGLISH 1"}
    assert PG.pick_page(f, "How big is the sports hall?")[0] == 0
    monkeypatch.setattr(T, "text_layer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read twice")))
    assert PG.pick_page(f, "How many doors does English 1 have?")[0] == 1  # from the cache


def test_sheet_storey_from_a_title_that_ends_with_the_floor():
    """P1 20:15: the EFA sheets' titles say 'Secondary Type 3 600 Ground Floor' (no 'plan'): the storey of a scanned
    set's page, so 'on the ground floor' finds it. A table listing every floor names none."""
    from stoa.formats.pages import sheet_storey

    assert sheet_storey("Plant Room\nSecondary Type 3 600 Ground Floor") == "Ground Floor"
    assert sheet_storey("x\nLevel 2") == "Level 2"
    assert sheet_storey("GROUND FLOOR PLAN\nFIRST FLOOR") == "GROUND"  # the plan title wins
    assert sheet_storey("FIRST FLOOR = 48 DORMS\nSECOND FLOOR\nTHIRD FLOOR") is None
    assert sheet_storey("the ground floor slab is cast in place before the first floor walls go up, see note") is None


def test_ask_context_points_follow_the_solved_copy(monkeypatch):
    """F7 20:24 / W5's feature registry: a right-click point / region arrives in the UPLOADED file's coordinates, but an
    upload > 6000 px is read through its smaller copy, a scanned PDF page through its PNG, an SVG through its PDF copy.
    ask_context_on(prep) hands the readers the point in the solved file's space, drops what it cannot map, restores."""
    import json
    import os

    from stoa.formats import normalize as N

    shrink = N.Prepared(original="/u/big.png", path="/u/big.s6000.png", kind="shrink", img_scale=0.5)
    assert N.to_solved(shrink, [[100, 40]], "image_px") == ([[50.0, 20.0]], "image_px")
    assert N.to_solved(shrink, [[100, 40]], "pdf_pt") is None
    scan = N.Prepared(original="/u/s.pdf", path="/u/s.scan.png", kind="scan", scan_zoom=4.0)
    assert N.to_solved(scan, [[10, 20]], "pdf_pt") == ([[40.0, 80.0]], "image_px")
    svg = N.Prepared(original="/u/a.svg", path="/u/a.svg-v2.pdf", kind="svg_pdf", svg_frame=(100.0, 50.0, 2.0))
    assert N.to_solved(svg, [[110, 70]], "svg") == ([[5.0, 10.0]], "pdf_pt")
    assert N.to_solved(N.Prepared(original="/u/x.pdf", path="/u/x.pdf"), [[1, 2]], "pdf_pt") == ([[1.0, 2.0]], "pdf_pt")

    ctx = {"point": [100, 40], "region": [[0, 0], [200, 0], [200, 80]], "space": "image_px", "view": "plan"}
    monkeypatch.setenv("STOA_ASK_CONTEXT", json.dumps(ctx))
    with N.ask_context_on(shrink):
        got = json.loads(os.environ["STOA_ASK_CONTEXT"])
        assert got["point"] == [50.0, 20.0] and got["region"][1] == [100.0, 0.0] and got["view"] == "plan"
        assert got["solved_file"] == "/u/big.s6000.png"
    assert json.loads(os.environ["STOA_ASK_CONTEXT"]) == ctx  # restored

    pages = N.Prepared(original="/u/set.pdf", path="/u/set.p2.pdf", kind="page", page=2, pages=3)
    monkeypatch.setenv("STOA_ASK_CONTEXT", json.dumps({"point": [5, 5], "space": "pdf_pt", "page": 0}))
    with N.ask_context_on(pages):  # the visitor clicked page 1, the question was read on page 3: not passed on
        got = json.loads(os.environ["STOA_ASK_CONTEXT"])
        assert "point" not in got and "page 3" in got["dropped"][0]
    monkeypatch.setenv("STOA_ASK_CONTEXT", json.dumps({"point": [5, 5], "space": "pdf_pt", "page": 2}))
    with N.ask_context_on(pages):
        got = json.loads(os.environ["STOA_ASK_CONTEXT"])
        assert got["point"] == [5.0, 5.0] and got["page"] == 0


def test_scanned_set_pages_classified_from_the_warm_reading(tmp_path):
    """T3's project loop: stoa.sheet.sets.classify reads a PDF page's own text, so every page of a SCANNED set came out
    'unknown, level not read' (P1's 3-page EFA scan: 3 of 3). pages.classify_scan_page uses what the warm already read
    (page texts cached in <stem>.pagetext.json; the page's scan PNG layer when cached) and never reads anything itself."""
    import json

    import fitz
    from PIL import Image

    from stoa.formats import pages as P

    img = tmp_path / "pic.png"
    Image.new("L", (400, 300), 255).save(img)
    pdf = tmp_path / "set.pdf"
    d = fitz.open()
    for _ in range(2):
        pg = d.new_page(width=842, height=595)
        pg.insert_image(pg.rect, filename=str(img))
    d.save(pdf)
    assert P.classify_scan_page(pdf, 0) is None  # nothing read yet: no cold read here
    texts = {"0": "SCHOOL\nSecondary Type 3 600 Ground Floor\nScale 1:200 @ A1", "1": "Second Floor Plan\nHall"}
    P._scan_text_file(pdf).write_text(json.dumps({"key": P._scan_key(pdf), "texts": texts}))
    a, b = P.classify_scan_page(pdf, 0), P.classify_scan_page(pdf, 1)
    assert (a.storey, a.page, str(a.file)) == ("0", 0, str(pdf)) and "Ground" in a.storey_name
    assert b.storey == "2" and b.kind == "floor_plan"


def test_private_hosts_are_an_explicit_list():
    """T2 20:43: ipaddress.is_private also admits documentation / reserved ranges (TEST-NET 192.0.2/24, 240/4): those
    are not our servers."""
    from stoa import llm

    for ok in ("http://127.0.0.1:8001/v1", "http://10.1.2.3:8110", "http://192.168.1.5", "http://100.101.2.3:8001",
               "http://[::1]:8001", "http://localhost:8001"):
        assert llm._private_host(ok), ok
    for bad in ("http://192.0.2.10/v1", "http://198.51.100.7", "http://203.0.113.9", "http://240.0.0.1",
                "https://generativelanguage.googleapis.com/v1beta/openai/", "http://8.8.8.8"):
        assert not llm._private_host(bad), bad

    class PrivateNetworkBlocked(ConnectionRefusedError):
        pass

    try:
        try:
            raise PrivateNetworkBlocked("no")
        except PrivateNetworkBlocked as inner:
            raise RuntimeError("connection error") from inner
    except RuntimeError as outer:
        assert llm._netguard_blocked(outer)
    assert not llm._netguard_blocked(RuntimeError("x"))


def test_cover_page_words_do_not_beat_the_plan_page(tmp_path):
    """S0 21:16 (d27 up_fmt2 pdf3_last / pdfmulti: 3 L): a cover sheet 'APARTMENT BUILDING - TENDER DRAWINGS / 1 Cover
    2 Legend 3 Floor plan' won 'net floor area of apartment C' with 2 weak word matches over the plan page (French
    labels, 48 area labels). Weak matches on a page with no drawing do not beat a page that carries one."""
    import fitz

    pdf = tmp_path / "set.pdf"
    d = fitz.open()
    cover = d.new_page(width=842, height=595)
    cover.insert_text((60, 80), "APARTMENT BUILDING - TENDER DRAWINGS", fontsize=20)
    cover.insert_text((60, 120), "1  Cover    2  Legend and notes    3  Floor plan", fontsize=11)
    legend = d.new_page(width=842, height=595)
    legend.insert_text((60, 80), "LEGEND", fontsize=14)
    legend.insert_text((60, 110), "Wall, load-bearing: solid fill. All dimensions in metres unless noted.", fontsize=9)
    plan = d.new_page(width=842, height=595)
    for i in range(12):
        x, y = 60 + (i % 4) * 180, 60 + (i // 4) * 160
        plan.draw_rect(fitz.Rect(x, y, x + 170, y + 150), width=2)
        for k in range(40):  # hatching: vector content like a real plan
            plan.draw_line((x + 4 * k % 170, y), (x + 4 * k % 170, y + 6))
        plan.insert_text((x + 10, y + 30), ["Cuisine", "Chambre", "Bain", "Séjour"][i % 4], fontsize=8)
        plan.insert_text((x + 10, y + 45), f"{10 + i}.{i:02d} m²", fontsize=8)
    d.save(pdf)
    from stoa.formats import pages as P

    k, why = P.pick_page(pdf, "What is the net floor area of apartment C in m², summing all its rooms except balconies?")
    assert k == 2, why
    # a question that names the cover's own words strongly still goes there (no quantity asked)
    k2, _ = P.pick_page(pdf, "What does the cover sheet say about the tender drawings?")
    assert k2 == 0


def _cad_pdf(path, lines=1500):
    import pymupdf

    d = pymupdf.open()
    pg = d.new_page(width=842, height=595)
    sh = pg.new_shape()
    for i in range(lines):
        sh.draw_line((20 + i % 800, 20 + (i * 7) % 550), (40 + i % 780, 30 + (i * 11) % 540))
    sh.finish(width=0.3)
    sh.commit()
    pg.insert_text((60, 60), "LAUNDRY 7.03 m2   KITCHEN 35.61 m2   SCALE 1:200 @ A1")
    d.save(str(path))
    return str(path)


def test_native_vector_pdf_tells_cad_exports_from_scans_and_traced_copies(tmp_path):
    import pymupdf
    from PIL import Image

    cad = _cad_pdf(tmp_path / "plan.pdf")
    assert normalize.native_vector_pdf(cad)
    # the same drawing as one of our traced copies (by name, or by producer after a rename) is not native
    traced = tmp_path / "plan.vec6.pdf"
    traced.write_bytes(open(cad, "rb").read())
    assert not normalize.native_vector_pdf(traced)
    d = pymupdf.open(cad)
    d.set_metadata({"producer": "stoa.formats.vectorize"})
    d.save(str(tmp_path / "renamed.pdf"))
    assert not normalize.native_vector_pdf(tmp_path / "renamed.pdf")
    # a scan: one page-sized image, even with an OCR text layer on top
    Image.new("L", (1200, 850), 255).save(tmp_path / "scan.png")
    s = pymupdf.open()
    pg = s.new_page(width=842, height=595)
    pg.insert_image(pg.rect, filename=str(tmp_path / "scan.png"))
    pg.insert_text((60, 60), "LAUNDRY 7.03 m2   KITCHEN 35.61 m2   SCALE 1:200 @ A1", render_mode=3)
    s.save(str(tmp_path / "scan.pdf"))
    assert not normalize.native_vector_pdf(tmp_path / "scan.pdf")
    # a text-only page (a cover / schedule without linework), images, missing files: False, never an error
    t = pymupdf.open()
    t.new_page().insert_text((60, 60), "APARTMENT BUILDING - TENDER DRAWINGS, SHEET INDEX")
    t.save(str(tmp_path / "cover.pdf"))
    assert not normalize.native_vector_pdf(tmp_path / "cover.pdf")
    assert not normalize.native_vector_pdf(tmp_path / "scan.png")
    assert not normalize.native_vector_pdf(tmp_path / "missing.pdf")
    assert not normalize.native_vector_pdf(cad, page=3)
    # our paper / page copies draw the original page as a Form XObject (A/B 21:55: EFA '.paperA1.pdf' rows kept paying
    # for the cross-check): the drawing inside still counts, and a wrapped scan is still a scan
    for src, want in ((cad, True), (str(tmp_path / "scan.pdf"), False)):
        w = pymupdf.open()
        w.new_page(width=1684, height=1190).show_pdf_page(pymupdf.Rect(0, 0, 1684, 1190), pymupdf.open(src), 0)
        out = tmp_path / f"wrapped_{want}.paperA1.pdf"
        w.save(str(out))
        assert normalize.native_vector_pdf(out) is want


def test_a_question_naming_a_schedule_goes_to_the_schedule_page(tmp_path):
    # X1 21:22 (night-X1 sl-249/254/255): 'according to the door schedule …' was answered from the plan page, which
    # prints the module names more often than the A-602 DOOR SCHEDULE page
    import pymupdf

    from stoa.formats import pages

    d = pymupdf.open()
    plan = d.new_page(width=842, height=595)
    sh = plan.new_shape()
    for i in range(1500):
        sh.draw_line((20 + i % 800, 20 + (i * 7) % 550), (40 + i % 780, 30 + (i * 11) % 540))
    sh.finish(width=0.3)
    sh.commit()
    y = 40
    for ln in ("MODULE 07 - SUPPORT - FLOOR PLAN", "MODULE 05 - RESTROOMS - FLOOR PLAN", "SUPPORT 7.01", "SUPPORT 7.02",
               "RESTROOMS 5.01", "SEE DOOR SCHEDULE A-602", "A-103"):
        plan.insert_text((60, y), ln)
        y += 14
    sched = d.new_page(width=842, height=595)
    y = 40
    for ln in ("A-602 DOOR SCHEDULE", "DOOR NUMBER  TYPE  REMARKS", "7.00A  A  SUPPORT", "7.01A  B", "5.01A  A  RESTROOMS"):
        sched.insert_text((60, y), ln)
        y += 14
    f = str(tmp_path / "set.pdf")
    d.save(f)
    k, why = pages.pick_page(f, "According to the door schedule, how many doors does the support module (module 07) have?")
    assert k == 1 and "DOOR SCHEDULE" in why, why
    assert pages.pick_page(f, "How many doors are listed in the door schedule?")[0] == 1
    # the plan's own questions stay on the plan; a schedule the set does not print changes nothing
    assert pages.pick_page(f, "How many doors are there in module 07?")[0] == 0
    assert pages.pick_page(f, "According to the window schedule, how many windows are in module 07?")[0] == 0


def test_a_schedule_question_on_a_picture_reads_the_schedule_as_a_table(monkeypatch):
    # X1 21:22: a SCANNED A-602 door schedule was counted as door symbols (vlm_lora 0); its table is read from the
    # image's OCR text on a traced copy instead (sc-289 0 -> 2 = gold)
    from types import SimpleNamespace

    from stoa.solvers import upload as U
    from stoa.solvers.raster import textlayer as T

    items = [SimpleNamespace(text=t) for t in ("A-602", "DOOR SCHEDULE", "DOOR NUMBER", "7.00A", "SEE NOTES")]
    monkeypatch.setattr(T, "text_layer", lambda p, *a, **k: SimpleNamespace(items=items))
    prep = SimpleNamespace(path="/x/set.p2.scan.png", scan_zoom=2.45)
    img = Answer(value=0, unit="count", confidence=0.75, solver="pipeline/vlm_lora")
    q = "How many doors does the door schedule list for the restrooms module (module 05)?"
    assert U._schedule_image(prep, q, img) == "DOOR SCHEDULE"
    assert U._schedule_image(prep, "How many doors are on this plan?", img) is None  # no schedule named
    assert U._schedule_image(prep, "According to the window schedule, how many windows?", img) is None  # not printed
    table = Answer(value=2, unit="count", confidence=0.3, solver="pipeline/pdf_text")
    assert U._table_reading(table) and U._schedule_image(prep, q, table) is None  # a table reader already answered


def _scan_pdf(path, page_in, img_px):
    """A scanned PDF: one grey image of img_px pixels filling a page of page_in inches (no text, no paths)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", img_px, 255).save(buf, format="PNG")
    doc = pymupdf.open()
    pg = doc.new_page(width=page_in[0] * 72, height=page_in[1] * 72)
    pg.insert_image(pg.rect, stream=buf.getvalue())
    doc.save(path)
    return str(path)


def test_paper_of_a_scanned_pdf_from_its_image_pixels_or_its_page(tmp_path):
    """P1 22:40 (d29 scan_dev): the 6000-px render of a scanned PDF is untagged, so 4 scanned Stoa sheets were never
    traced. (1) the page image's own pixels + the printed size: S-100F scan 6623 x 4678 + 'ISO A1' -> A1 @200, the render
    at 6000 px -> 181.2 dpi; (2) no size printed: the page's own size when the image fills it at a scan dpi (MFC ANSI D
    @200, AFCEC Tabloid @300); an 'ISO A1' title block on an A3 page that is no A1 at its pixels = a reduced copy -> None."""
    from stoa.formats.paper import paper_of_scan

    a1 = _scan_pdf(tmp_path / "s100.pdf", (33.11, 23.39), (6623, 4678))
    dpi, sheet, how = paper_of_scan(a1, (6000, 4238), ["ISO A1", "A2", "SCALE:1:50"], use_page=False)
    assert sheet == "A1" and abs(dpi - 181.2) < 0.3 and "6623 x 4678" in how
    ansi = _scan_pdf(tmp_path / "mfc.pdf", (34, 22), (6800, 4400))
    assert paper_of_scan(ansi, (6000, 3883), ["FLOOR PLAN"], use_page=False) is None
    dpi, sheet, how = paper_of_scan(ansi, (6000, 3883), ["FLOOR PLAN", "A1"])  # a lone 'A1' is a grid reference
    assert sheet == "ANSI D" and abs(dpi - 176.5) < 0.3 and "page size" in how
    tab = _scan_pdf(tmp_path / "afcec.pdf", (17, 11), (5100, 3300))
    assert paper_of_scan(tab, (5100, 3300), ["SCALE : 1\" = 40'-0\""])[:2] == (300.0, "Tabloid")
    # an A3 page scanned at 300 dpi: 'A1' printed and A1 @150 fits its pixels -> A1 (the drawing's own paper)
    a3 = _scan_pdf(tmp_path / "efa.pdf", (16.54, 11.69), (4961, 3508))
    assert paper_of_scan(a3, (4961, 3508), ["1:200 @ A1", "A1"])[1] == "A1"
    assert paper_of_scan(a3, (4961, 3508), ["1:100"])[1] == "A3"
    # a reduced copy that names another ISO size its pixels do not fit: no guess
    a3b = _scan_pdf(tmp_path / "red.pdf", (16.54, 11.69), (3508, 2480))
    assert paper_of_scan(a3b, (3508, 2480), ["ISO A0"]) is None
    # custom page / an image covering part of the page / an odd scan resolution: no guess
    odd = _scan_pdf(tmp_path / "odd.pdf", (20, 13), (4000, 2600))
    assert paper_of_scan(odd, (4000, 2600), []) is None
    odd2 = _scan_pdf(tmp_path / "odd2.pdf", (34, 22), (6000, 3882))
    assert paper_of_scan(odd2, (6000, 3882), []) is None  # 176.5 dpi: a render, not a scanner's resolution


def test_prepare_records_the_scanned_pdf_it_rendered(tmp_path):
    pdf = _scan_pdf(tmp_path / "scan.pdf", (16.54, 11.69), (1654, 1169))
    prep = normalize.prepare(pdf, "How big is the kitchen?")
    assert prep.kind == "scan" and prep.scan_pdf == pdf and prep.path.endswith(".scan.png")


def test_paper_of_image_reads_a_scan_render_through_its_sidecar(tmp_path, monkeypatch):
    """Readers that only get the render (openings, takeoff) find the scanned PDF via '<render>.json' (scan_png)."""
    from PIL import Image

    from stoa.formats.paper import paper_of_image

    pdf = _scan_pdf(tmp_path / "s100.pdf", (33.11, 23.39), (6623, 4678))
    png, zoom = normalize.scan_png(pdf, max_side=3000)  # 72 dpi at least: 2384 px
    w = Image.open(png).size[0]
    assert w < 6623
    dpi, sheet = paper_of_image(png, ["ISO A1"])
    assert sheet == "A1" and abs(dpi - w / 33.11) < 0.2
    assert paper_of_image(png, ["1:50"]) is None  # no size printed: the pixel rule stays strict
    monkeypatch.setenv("STOA_SCAN_PAPER", "0")
    assert paper_of_image(png, ["ISO A1"]) is None


def test_traced_copy_keeps_a_piping_span_but_not_ocr_area_tags():
    """P1 22:50: on a traced scan the piping reader's tag-to-tag span is a measurement (scanned M-101A 4/4 = gold);
    its other answers and area_tags (3 right / 14 wrong on traced copies) are not."""
    from stoa.solvers.upload import _measured

    span = Answer(value="6.5 ft", confidence=0.82, solver="pipeline/piping",
                  trace=["parsed scale: 18 PDF points per foot", "measured tag-center span: 117.00 PDF points"])
    assert _measured(span)
    assert not _measured(Answer(value="16 inches", confidence=0.95, solver="pipeline/piping", trace=[]))
    assert not _measured(Answer(value="6.5 ft", confidence=0.82, solver="pipeline/piping", trace=["read a label"]))
    assert not _measured(Answer(value=362.0, confidence=0.6, solver="pipeline/area_tags", trace=[]))
    assert _measured(Answer(value=3.17, confidence=0.4, solver="pipeline/line_rooms", trace=[]))
    # door swings traced at 1:480 are glyph-sized (AFCEC PNG: 494 doors vs 81 on its PDF twin); 1:200 stays
    big = ["[openings] openings: 592 door-swing arcs on the vector page (printed scale 1:480)"]
    assert not _measured(Answer(value=494, confidence=0.55, solver="pipeline/openings", trace=big))
    ok = ["[openings] openings: 115 door-swing arcs on the vector page (printed scale 1:200)"]
    assert _measured(Answer(value=98, confidence=0.5, solver="pipeline/openings", trace=ok))


def test_warm_traces_a_scanned_pdf_whose_paper_is_known(tmp_path, monkeypatch):
    """P1 22:50: the upload pre-read traces a scanned PDF page with the paper the question's _as_vector will use
    (upload.trace_paper), so the first area question does not pay the trace."""
    from stoa.formats import vectorize as V
    from stoa.formats import warm as W
    from stoa.solvers.raster import textlayer as T

    items = [T.TextItem(text="ISO A1", box=[10, 10, 60, 20])]
    monkeypatch.setattr(T, "text_layer", lambda p, *a, **k: T.TextLayer(path=p, size=(1, 1), items=items))
    monkeypatch.setattr(T, "is_clean", lambda p: False)
    traced = []
    monkeypatch.setattr(V, "traced_copy", lambda png, dpi: traced.append((png, dpi)) or png)
    pdf = _scan_pdf(tmp_path / "s100.pdf", (33.11, 23.39), (3311, 2339))  # A1 scanned at 100 dpi
    out = W.warm(pdf)
    assert out["error"] == "" and out["kind"] == "scan"
    assert len(traced) == 1 and traced[0][0].endswith(".scan.png")
    assert any(d.startswith("vector copy of the scan (A1 at 100 dpi") for d in out["done"]), out["done"]


def test_over_budget_traced_pass_tells_the_user_to_ask_again(tmp_path, monkeypatch):
    """X1 22:22: the first question on a new sheet runs over the traced-copy budget and gets the image's weak guess;
    the card now says the drawing's lines are still being measured (a re-ask reads the warm copy)."""
    from stoa.formats import vectorize as V
    from stoa.solvers import upload as U

    png = tmp_path / "sheet.png"
    png.write_bytes(b"x")
    monkeypatch.setattr(U, "trace_paper", lambda prep: ((150.0, "A3"), "from its metadata"))
    monkeypatch.setattr(V, "traced_copy", lambda p, dpi: str(tmp_path / "sheet.vec6.pdf"))
    calls = []  # 1st = the trace (done), 2nd = the traced copy's readers (over their budget)
    monkeypatch.setattr(U, "_solve_within", lambda fn, budget, late=None: calls.append(budget) or
                        ((fn(), 0.1) if len(calls) == 1 else (None, budget)))
    prep = normalize.Prepared(original=str(png), path=str(png))
    ans0 = Answer(value=26.5, unit="m2", confidence=0.3, solver="pipeline/raster",
                  trace=["intent: area slab [drawing] (lang en)"])
    out = U.Upload._as_vector(object.__new__(U.Upload), prep, "What is the slab area?", ans0)
    assert out.value == 26.5 and "ask again in about a minute" in out.missing[0]
    assert any("no reading within" in str(t) for t in out.trace)
    assert calls == [U.TRACE_WAIT_S, U.VECTOR_BUDGET_S]


def test_a_long_trace_does_not_hold_the_question(tmp_path, monkeypatch):
    """P1 00:40 (S0 d30 wild-sealed): a 14 MB scan traced for 132 s BEFORE the readers' 30 s budget, so each of its 4
    questions took 184-210 s (d29: 4-8 s). The wait for the trace is bounded (STOA_TRACE_WAIT); the image answer
    stands with the 'ask again' note and the trace finishes in the background."""
    import threading
    import time

    from stoa.formats import vectorize as V
    from stoa.solvers import upload as U

    png = tmp_path / "scan.png"
    png.write_bytes(b"x")
    go, done = threading.Event(), threading.Event()
    monkeypatch.setattr(U, "trace_paper", lambda prep: ((100.0, "A1"), "from its pixels"))
    monkeypatch.setattr(V, "traced_copy", lambda p, dpi: go.wait(5) and done.set() or str(tmp_path / "scan.vec6.pdf"))
    monkeypatch.setattr(U, "TRACE_WAIT_S", 0.2)
    (tmp_path / "scan.vec6.pdf").write_bytes(b"%PDF")
    read, seen = threading.Event(), []
    monkeypatch.setattr(U.Pipeline, "solve", lambda self, p, q: seen.append((go.is_set(), p)) or read.set() or
                        Answer(value=None, solver="pipeline/prior"))
    monkeypatch.setattr(U, "_VEC_NONE", {})
    prep = normalize.Prepared(original=str(png), path=str(png))
    ans0 = Answer(value=174.0, unit="ft2", confidence=0.2, solver="pipeline/vlm_direct",
                  trace=["intent: area bedroom [drawing] (lang en)"])
    t0 = time.perf_counter()
    out = U.Upload._as_vector(object.__new__(U.Upload), prep, "How big is bedroom A?", ans0)
    assert time.perf_counter() - t0 < 2.0
    assert out.value == 174.0 and out.missing[0] == U.TRACE_NOTE
    assert any("still being made after 0 s" in str(t) for t in out.trace), out.trace
    assert not seen  # no readers before the copy exists
    go.set()
    assert done.wait(5) and read.wait(5)  # the trace ran on in its daemon thread, then the copy's readers
    assert seen == [(True, str(tmp_path / "scan.vec6.pdf"))]
    time.sleep(0.1)
    assert list(U._VEC_NONE.values()) == ["no measured reading (pipeline/prior; read after a late trace)"]


def test_warm_traces_after_releasing_the_question_lock(tmp_path, monkeypatch):
    """P1 00:40: a question waits for warm (wait_for_warm) but not for warm's trace: the trace runs unlocked."""
    import fcntl

    from stoa.formats import vectorize as V
    from stoa.formats import warm as W
    from stoa.solvers.raster import textlayer as T

    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    items = [T.TextItem(text="ISO A1", box=[10, 10, 60, 20])]
    monkeypatch.setattr(T, "text_layer", lambda p, *a, **k: T.TextLayer(path=p, size=(1, 1), items=items))
    monkeypatch.setattr(T, "is_clean", lambda p: False)
    pdf = _scan_pdf(tmp_path / "s100.pdf", (33.11, 23.39), (3311, 2339))
    held = []

    def trace(png, dpi):
        with open(W._lock_path(pdf), "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                held.append(False)
            except OSError:
                held.append(True)
        return png

    monkeypatch.setattr(V, "traced_copy", trace)
    out = W.warm(pdf)
    assert held == [False] and out["error"] == ""
    assert any(d.startswith("vector copy of the scan (A1 at 100 dpi") for d in out["done"]), out["done"]


# --- captain 23:04 (Ziying): count small symbols on the FULL-resolution original, not the 6000 px copy ----------------
def _sheet(path, size, dpi=None):
    from PIL import Image, ImageDraw

    im = Image.new("L", size, 255)
    d = ImageDraw.Draw(im)
    for i in range(0, size[0], max(1, size[0] // 20)):
        d.line([(i, 0), (i, size[1] - 1)], fill=0, width=1)
    im.save(path, **({"dpi": (dpi, dpi)} if dpi else {}))
    return path


def test_full_resolution_finds_the_original_of_both_copy_kinds(tmp_path):
    big = _sheet(tmp_path / "a.png", (3000, 2000), dpi=300)
    # eval / CLI copy next to its original ('<stem>.s6000.png' + sidecar)
    r = normalize.shrink_image(big, tmp_path / "a.s1500.png", 1500)
    assert r["factor"] == pytest.approx(0.5)
    full, k = normalize.full_resolution(tmp_path / "a.s1500.png")
    assert full == str(big) and k == pytest.approx(2.0)
    # web upload: shrunk in place, the original kept as '<stem>.full<ext>'
    up = tmp_path / "up"
    up.mkdir()
    _sheet(up / "b.full.png", (3000, 2000))
    normalize.shrink_image(_sheet(up / "b.png", (3000, 2000)), up / "b.png", 1200)
    full, k = normalize.full_resolution(up / "b.png")
    assert full.endswith("b.full.png") and k == pytest.approx(2.5)
    # an image that is its own original; a '.full' sibling of another shape is not taken
    assert normalize.full_resolution(big) == (str(big), 1.0)
    _sheet(tmp_path / "c.full.png", (3000, 3000))
    _sheet(tmp_path / "c.png", (1200, 800))
    assert normalize.full_resolution(tmp_path / "c.png")[1] == 1.0


def test_crop_full_cuts_the_original_and_maps_points_back(tmp_path):
    from PIL import Image

    big = _sheet(tmp_path / "a.png", (3000, 2000), dpi=300)
    normalize.shrink_image(big, tmp_path / "a.s1500.png", 1500)
    c = normalize.crop_full(tmp_path / "a.s1500.png", (100, 200, 400, 500), pad=10)
    assert c["k"] == pytest.approx(2.0) and c["offset"] == (90.0, 190.0) and c["size"] == (640, 640)
    with Image.open(c["path"]) as im:
        assert im.size == (640, 640) and round(im.info["dpi"][0]) == 300
    assert normalize.to_path_px(c, [(0, 0), (640, 640), (20, 20, 40, 40)]) == [
        (90.0, 190.0), (410.0, 510.0), (100.0, 200.0, 110.0, 210.0)]
    small = normalize.crop_full(tmp_path / "a.s1500.png", (0, 0, 1500, 1000), max_side=2000)
    assert small["k"] == pytest.approx(2000 / 1500) and small["size"] == (2000, 1333)
    with pytest.raises(ValueError):
        normalize.crop_full(tmp_path / "a.s1500.png", (5, 5, 5, 5))
    own = normalize.crop_full(big, (0, 0, 100, 100))
    assert own["k"] == 1.0 and "itself" in own["note"]


def test_crop_full_never_writes_next_to_the_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    up = tmp_path / "up"
    up.mkdir()
    _sheet(up / "b.full.png", (3000, 2000))
    normalize.shrink_image(_sheet(up / "b.png", (3000, 2000)), up / "b.png", 1200)
    c = normalize.crop_full(up / "b.png", (10, 10, 200, 200))
    assert c["k"] == pytest.approx(2.5) and not c["path"].startswith(str(up))
    assert sorted(p.name for p in up.iterdir()) == ["b.full.png", "b.png"]


def test_svg_ifc_plural_area_words_and_slab_areas_are_not_room_sums():
    from stoa.solvers.svg_ifc import _AREA, _VOL

    assert _AREA.search("Sum of all slab areas?") and _AREA.search("Wie groß sind die Flächen?")
    assert _VOL.search("Total volumes of the slabs?") and not _AREA.search("How many cubicles?")
    import os

    f = os.path.join(os.environ.get("STOA_PREVIEW", ""), "data/ifc_models/kit-fzk-haus_ac20-fzk-haus_plan-02-dachgeschoss.svg")
    if not os.path.exists(f):
        pytest.skip("preview dataset not here")
    from stoa.solvers.svg_ifc import SvgIfc

    for q in ("Sum of all slab areas?", "Sum of all slab area?"):  # H 23:52: was the ROOMS' sum @0.85 (singular)
        a = SvgIfc().solve(f, q)
        assert a.value is None and any("IFC model" in m for m in a.missing)
    assert SvgIfc().solve(f, "What are the room areas in total?").value == pytest.approx(86.36, abs=0.01)


def test_16bit_grey_pictures_and_tiffs_keep_their_greys(tmp_path, monkeypatch):
    """P1 c15: Pillow 13 converts 16-bit grey ('I;16') to 'L' / 'RGB' by CLIPPING (grey 50 -> 255), so a scanner's
    16-bit PNG / TIFF reached every PIL reader with its mid-grey ink gone. prepare() reads them (and TIFF / WebP / BMP /
    GIF on the CLI path) from an 8-bit copy in the side cache with the same pixels; evidence goes back to the upload."""
    from pathlib import Path

    import numpy as np
    from PIL import Image

    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    g = np.tile(np.array([0, 50, 128, 200, 255], dtype=np.uint16), (40, 10)) * 257
    png, tif, plain = tmp_path / "a" / "s.png", tmp_path / "b" / "s.tif", tmp_path / "c" / "s.png"
    for p in (png, tif, plain):
        p.parent.mkdir()
    Image.fromarray(g).save(png)
    Image.fromarray(g).save(tif, compression="tiff_lzw")
    Image.fromarray((g // 257).astype(np.uint8)).save(plain)
    assert list(np.asarray(Image.open(png).convert("L"))[0, :5]) == [0, 255, 255, 255, 255]  # the clipping itself
    for p in (png, tif):
        prep = normalize.prepare(str(p), "How big is room 1?")
        assert prep.kind == "picture" and "8-bit PNG" in prep.notes[0]
        assert list(np.asarray(Image.open(prep.path).convert("L"))[0, :5]) == [0, 50, 128, 200, 255]
        assert [f.name for f in p.parent.iterdir()] == [p.name]  # nothing written next to the upload
        ans = Answer(value=1.0, evidence=[Evidence(file=prep.path, bbox=[1, 2, 3, 4], bbox_space="image_px")])
        normalize.restore(ans, prep)
        assert Path(ans.evidence[0].file).name == p.name and ans.evidence[0].bbox == [1, 2, 3, 4]
    assert normalize.prepare(str(plain), "q").kind == "as_is"  # 8-bit PNGs untouched
    monkeypatch.setenv("STOA_PICTURE_PNG", "0")
    assert normalize.prepare(str(png), "q").kind == "as_is"  # kill switch
    big = tmp_path / "d" / "big.png"
    big.parent.mkdir()
    Image.fromarray(np.tile(g, (1, 20))).save(big)  # 1000 px wide -> shrunk to 500
    r = normalize.shrink_image(big, tmp_path / "d" / "big.s500.png", 500)
    assert r["factor"] == 0.5  # mean grey of the pattern 0/50/128/200/255 = 127; clipped it would be ~204
    assert np.asarray(Image.open(r["path"]).convert("L")).mean() < 160


def test_transparent_png_is_read_on_white_on_the_cli_path(tmp_path, monkeypatch):
    """F6 01:17: a CAD export's transparent PNG (black RGB under alpha 0) read as a BLACK sheet on the CLI / API path."""
    import numpy as np
    from PIL import Image

    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    g = np.full((50, 60), 255, np.uint8)
    g[10:20, :] = 0
    t, o = tmp_path / "t.png", tmp_path / "o.png"
    Image.fromarray(np.dstack([np.zeros_like(g)] * 3 + [255 - g]), "RGBA").save(t)
    Image.fromarray(np.dstack([g] * 3 + [np.full_like(g, 255)]), "RGBA").save(o)
    prep = normalize.prepare(str(t), "q")
    assert prep.kind == "picture" and "transparent" in prep.notes[0]
    assert list(np.asarray(Image.open(prep.path).convert("L"))[[0, 15], 0]) == [255, 0]
    assert normalize.prepare(str(o), "q").kind == "as_is"  # opaque RGBA: untouched


def test_dwg_text_cut_inside_a_utf8_character_is_rejoined_and_a_failed_save_keeps_the_file(tmp_path, monkeypatch):
    """QA 02:02: LibreDWG cut an MTEXT chunk inside the bullet's UTF-8 bytes ('\\xe2\\x80' | '\\xa2'); ezdxf gave lone
    surrogates, PyMuPDF raised and the whole DWG upload failed. And an ezdxf save that failed half-way left 0 bytes."""
    import ezdxf

    raw = b"  0\r\nMTEXT\r\n  3\r\nNOTES \xe2\x80\r\n  1\r\n\xa2 SALVAGE\r\n  1\r\nok \xc3\xa4\r\n"
    out, n = dwg._rejoin_utf8(raw)
    assert n == 1 and out == b"  0\r\nMTEXT\r\n  3\r\nNOTES \r\n  1\r\n\xe2\x80\xa2 SALVAGE\r\n  1\r\nok \xc3\xa4\r\n"
    assert dwg._rejoin_utf8(out) == (out, 0)
    doc = ezdxf.new("R2000")
    t = doc.modelspace().add_text("App. A", dxfattribs={"insert": (5, 5), "halign": 1, "align_point": (0, 0)})
    assert t.dxf.align_point == (0, 0, 0)
    p = tmp_path / "x.dxf"
    doc.saveas(p)
    size = p.stat().st_size

    def boom(self, *a, **k):
        raise AttributeError("'str' object has no attribute 'dxf'")

    monkeypatch.setattr(ezdxf.document.Drawing, "saveas", boom)
    assert dwg.fix_converted(p) == [] and p.stat().st_size == size  # nothing claimed, file intact
    monkeypatch.undo()
    assert dwg.fix_converted(p) == ["1 aligned texts placed at their insert point"]
    assert ezdxf.readfile(p).modelspace().query("TEXT")[0].dxf.align_point == (5, 5, 0)
