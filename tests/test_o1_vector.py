"""O1: PDF-only floor model (stoa/solvers/vector/pdffloor.py) path parsing."""

from types import SimpleNamespace as NS

from stoa.solvers.vector.pdffloor import _pts, _rings


def P(x, y):
    return NS(x=x, y=y)


def R(x0, y0, x1, y1):
    return NS(x0=x0, y0=y0, x1=x1, y1=y1)


def test_room_path_with_extra_subpaths_keeps_the_outline():
    # a 10 x 10 room drawn as 4 lines, then two tiny rectangles in the same path (site1176 floor10837's corridors:
    # the old reader kept the LAST subpath, so the room became a 0.03 m² box and lost its label)
    outline = [("l", P(0, 0), P(10, 0)), ("l", P(10, 0), P(10, 10)), ("l", P(10, 10), P(0, 10)),
               ("l", P(0, 10), P(0, 0))]
    d = {"items": outline + [("re", R(2, 2, 2.5, 2.5)), ("re", R(7, 7, 7.2, 7.2))]}
    assert len(_rings(d)) == 3
    pts = _pts(d)
    assert len(pts) == 5 and (10, 10) in pts


def test_disjoint_line_subpaths_split():
    a = [("l", P(0, 0), P(1, 0)), ("l", P(1, 0), P(1, 1)), ("l", P(1, 1), P(0, 0))]
    b = [("l", P(5, 5), P(9, 5)), ("l", P(9, 5), P(9, 9)), ("l", P(9, 9), P(5, 5))]
    d = {"items": a + b}
    assert len(_rings(d)) == 2
    assert (9, 9) in _pts(d)


def test_single_rect_and_short_paths():
    assert _pts({"items": [("re", R(0, 0, 2, 3))]}) == [(0, 0), (2, 0), (2, 3), (0, 3)]
    assert _pts({"items": [("l", P(0, 0), P(1, 0))]}) == []


def test_pdf_textqa_text_layer_from_the_pdf(tmp_path, monkeypatch):
    import pymupdf

    from stoa.solvers.vector import pdf_textqa as Q

    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path))
    pdf = tmp_path / "sheet.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((100, 100), "21 Stg.", fontsize=10)
    pg.insert_text((300, 500), "BRH 87.50", fontsize=10)
    doc.save(str(pdf))
    png, tl, s = Q.render_with_text(str(pdf))
    assert abs(s - 150 / 72) < 1e-6 and {i.text for i in tl.items} == {"21 Stg.", "BRH 87.50"}
    it = next(i for i in tl.items if i.text == "21 Stg.")
    assert 95 * s < it.cx < 160 * s  # boxes in image px of the rendered page
    from stoa.contract import Answer, Evidence

    a = Answer(value=21, unit="count", confidence=0.6, solver="pdf_textqa",
               evidence=[Evidence(file=png, page=0, bbox=it.box, bbox_space="image_px", note="text '21 Stg.'")])
    Q._to_pdf(a, str(pdf), png, 0, s)
    e = a.evidence[0]
    assert e.file == str(pdf) and e.bbox_space == "pdf_pt" and e.exact and 95 < e.bbox[0] < 110


def test_mtext_does_not_crash_the_floor_loader(tmp_path):
    """P1 15:10: ezdxf's dxf.get('height') raises on MTEXT (it has char_height); every real CAD DXF fell to the prior."""
    ezdxf = __import__("pytest").importorskip("ezdxf")
    from stoa.solvers.vector.floor import load_floor

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_mtext("KITCHEN", dxfattribs={"layer": "A-ANNO-TEXT", "char_height": 0.3, "insert": (1, 2)})
    msp.add_text("Wohnen", dxfattribs={"layer": "ROOM_TEXT", "height": 0.25, "insert": (3, 4)})
    p = tmp_path / "m.dxf"
    doc.saveas(p)
    fm = load_floor(str(p))
    assert fm is not None


def test_lift_counted_as_room_is_named_when_shafts_are_excluded():
    from stoa.solvers.vector.dxf_solver import _lift_caveats, _Part

    lift = NS(name="Lift", net=2.6, label=lambda: "6.17 'Lift'")
    room = NS(name="Zimmer", net=20.0, label=lambda: "6.01 'Zimmer'")
    p = _Part(22.6, 0.9, [], [])
    p.rooms = [room, lift]
    q = "Wie gross ist die Nettogeschossfläche dieses Geschosses (ohne Balkone, Schächte und Lufträume)?"
    out = _lift_caveats([p], q)
    assert len(out) == 1 and "20.00 m²" in out[0] and "Lift" in out[0]
    assert _lift_caveats([p], "Wie gross ist die Nettogeschossfläche (ohne Balkone)?") == []


def test_pdf_textqa_renders_a_large_sheet(tmp_path, monkeypatch):
    """A1 page: the render DPI falls below DPI and must be an int (pymupdf get_pixmap(dpi=float) raised TypeError)."""
    import pymupdf

    from stoa.solvers.vector import pdf_textqa as PT

    monkeypatch.setattr(PT, "_cache_dir", lambda: tmp_path)
    pdf = tmp_path / "a1.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=2384, height=1684)  # A1 landscape in pt
    pg.insert_text((100, 100), "LIVING ROOM 24.5 m2", fontsize=20)
    doc.save(str(pdf))
    png, tl, s = PT.render_with_text(str(pdf))
    assert max(tl.size) <= PT.MAX_SIDE + 1 and any("LIVING" in i.text for i in tl.items) and s > 0


def test_dxf_total_over_no_room_outlines_is_not_zero(tmp_path):
    """X1 dyimba (real CAD DXF without room polygons): '0 numbered indoor rooms = 0.00 m²' was answered at 0.96."""
    import ezdxf

    from stoa.solvers.vector.dxf_solver import DxfSolver

    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 8), (0, 8)], close=True, dxfattribs={"layer": "A-Wall"})
    msp.add_text("KITCHEN", dxfattribs={"layer": "A-Text", "height": 0.3}).set_placement((2, 2))
    p = tmp_path / "plan.dxf"
    doc.saveas(p)
    a = DxfSolver().solve(str(p), "What is the total floor area of this plan?")
    assert a.value is None or a.confidence <= 0.3
