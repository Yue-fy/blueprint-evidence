"""G1 c8: the visitor's click / region / view context (stoa/agent/click.py; captain 19:47 / 19:54, KNOWLEDGE 20:27)."""
import json
from types import SimpleNamespace

from stoa.agent import click as C
from stoa.contract import Answer, Evidence


def test_parse_shapes():
    a = C.Ask.parse({"point": [10, 20], "space": "pdf_pt", "page": 0})
    assert a.point == (10.0, 20.0) and a.page == 0 and a.space == "pdf_pt"
    b = C.Ask.parse(json.dumps({"context": {"region": [[0, 0], [10, 0], [10, 10]], "space": "image_px"}}))
    assert len(b.region) == 3 and b.space == "image_px"
    box = C.Ask.parse({"region": [0, 0, 10, 5], "space": "pdf_pt"})
    assert box.region == [(0, 0), (10, 0), (10, 5), (0, 5)]
    assert C.Ask.parse({"point": [1, 2], "space": "screen"}) is None  # unknown space: ignored, not guessed
    assert C.Ask.parse({}) is None and C.Ask.parse("not json") is None
    assert C.Ask.parse({"view": "MODULE 02 - FLOOR PLAN"}).view == "MODULE 02 - FLOOR PLAN"


def test_geometry():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert C.inside((5, 5), sq) and not C.inside((15, 5), sq)
    assert C.poly_area(sq) == 100
    assert abs(C.long_side([(0, 0), (20, 0), (20, 2), (0, 2)]) - 20) < 1e-9


def test_use_current_env(monkeypatch):
    monkeypatch.setenv("STOA_ASK_CONTEXT", json.dumps({"point": [1, 2], "space": "image_px"}))
    assert C.current().point == (1.0, 2.0)  # W5's upload worker
    with C.use(None):
        assert C.current() is None  # nested solves of a context answer see none
    with C.use({"point": [5, 6], "space": "pdf_pt"}):
        assert C.current().point == (5.0, 6.0)
    monkeypatch.delenv("STOA_ASK_CONTEXT")
    assert C.current() is None


def test_mapped_through_shrunk_copy():
    prep = SimpleNamespace(img_scale=0.5, scan_zoom=0.0, paper_f=0.0, photo_h=None, pages=1, page=0)
    with C.use({"point": [100, 50], "region": [[0, 0], [200, 0], [200, 100]], "space": "image_px"}):
        with C.mapped(prep):
            m = C.current()
            assert m.point == (50.0, 25.0) and m.region[1] == (100.0, 0.0)
    scan = SimpleNamespace(img_scale=0.0, scan_zoom=2.0, paper_f=0.0, photo_h=None, pages=1, page=0)
    with C.use({"point": [10, 10], "space": "pdf_pt"}), C.mapped(scan):
        assert C.current().space == "image_px" and C.current().point == (20.0, 20.0)
    other_page = SimpleNamespace(img_scale=0.0, scan_zoom=0.0, paper_f=0.0, photo_h=None, pages=3, page=1)
    with C.use({"point": [10, 10], "space": "pdf_pt", "page": 2}), C.mapped(other_page):
        assert C.current() is None  # drawn on another page than the one read


def _marks(n_in, n_out):
    ev = [Evidence(file="x.pdf", page=0, bbox=[10 + i, 10, 12 + i, 12], bbox_space="pdf_pt", note=f"door {i}")
          for i in range(n_in)]
    ev += [Evidence(file="x.pdf", page=0, bbox=[500 + i, 500, 502 + i, 502], bbox_space="pdf_pt", note="door")
           for i in range(n_out)]
    return ev


def test_region_count_keeps_items_inside():
    ctx = C.Ask(region=[(0, 0), (100, 0), (100, 100), (0, 100)], space="pdf_pt")
    base = Answer(value=5, unit="count", confidence=0.7, evidence=_marks(2, 3), solver="pipeline/openings",
                  trace=["openings: 5"])
    a = C._filter_count(base, "x.pdf", "How many doors are there?", ctx)
    assert a.value == 2 and a.confidence == 0.7
    assert any("2 of the reader's 5" in t for t in a.trace) and any("region you marked" in x for x in a.assumptions)
    # not one mark per item: the count is NOT limited, and the answer says so
    base2 = Answer(value=7, unit="count", confidence=0.6, evidence=_marks(2, 3), solver="pipeline/x", trace=[])
    b = C._filter_count(base2, "x.pdf", "How many doors are there?", ctx)
    assert b.value == 7 and b.confidence == 0.3 and any("could not be limited" in m for m in b.missing)


def test_point_picks_the_clicked_room_of_several():
    polys = [[[0, 0], [10, 0], [10, 10], [0, 10]], [[20, 0], [30, 0], [30, 10], [20, 10]]]
    ev = [Evidence(file="x.pdf", page=0, bbox=[p[0][0], 0, p[1][0], 10], bbox_space="pdf_pt", polygon=p,
                   note=f"Classroom: {v} m² traced at 1:200 (approximate)") for p, v in zip(polys, (52.13, 50.77))]
    a = Answer(value=51.45, unit="m2", confidence=0.45, evidence=ev, solver="pipeline/plan_rooms")
    v, e = C._at_point(a, (25, 5), "pdf_pt")
    assert v == 50.77 and e is ev[1]
    assert C._at_point(a, (15, 5), "pdf_pt") is None  # between the rooms: no pick


def test_region_area_and_line_at_user_scale(monkeypatch):
    pipe = SimpleNamespace(user_scale=100, px_per_m=None)
    monkeypatch.setattr(C, "tags", lambda path, page=0: [])
    ctx = C.Ask(region=[(0, 0), (72, 0), (72, 72), (0, 72)], space="pdf_pt")
    a = C.answer(pipe, "x.pdf", "What is the area of this region?", ctx, solve=lambda p, q: None)
    assert a.unit == "m2" and abs(a.value - 6.45) < 0.01 and a.evidence[0].polygon  # 1 in at 1:100 = 2.54 m square
    line = C.Ask(region=[(0, 0), (72, 0)], space="pdf_pt")
    b = C.answer(pipe, "x.pdf", "How long is this wall?", line, solve=lambda p, q: None)
    assert b.unit == "m" and abs(b.value - 2.54) < 1e-3 and b.evidence[0].polyline


def test_strip_scope_and_view():
    assert C._strip_scope("How many doors are in this region?") == "How many doors are there?"
    assert C._strip_scope("How many toilets are in here?") == "How many toilets are there?"
    assert C._with_view("How many doors?", "MODULE 02 ADMINISTRATION") == "How many doors in MODULE 02 ADMINISTRATION?"
    assert C._with_view("How many doors in module 02 administration?", "MODULE 02 ADMINISTRATION").endswith("tion?")


def test_count_like_clicked_symbol(tmp_path, monkeypatch):
    """5 copies of a small symbol (circle + cross; one touching a thick wall, one rotated 90°), 3 decoy squares:
    the clicked symbol's copies are counted, the decoys and the wall are not."""
    import pymupdf

    f = tmp_path / "sym.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    pg.draw_rect(pymupdf.Rect(40, 40, 560, 50), color=(0, 0, 0), fill=(0, 0, 0))  # a thick wall

    def sym(cx, cy, rot=False):
        pg.draw_circle((cx, cy), 6, color=(0, 0, 0), width=0.6)
        if rot:
            pg.draw_line((cx - 6, cy), (cx + 6, cy), width=0.6)
            pg.draw_line((cx + 6, cy), (cx + 6, cy - 9), width=0.6)
        else:
            pg.draw_line((cx, cy - 6), (cx, cy + 6), width=0.6)
            pg.draw_line((cx, cy + 6), (cx + 9, cy + 6), width=0.6)

    for cx, cy in ((100, 150), (200, 150), (300, 250), (450, 300)):
        sym(cx, cy)
    sym(150, 300, rot=True)
    for cx, cy in ((250, 150), (400, 150), (500, 220)):
        pg.draw_rect(pymupdf.Rect(cx - 6, cy - 6, cx + 6, cy + 6), color=(0, 0, 0), width=0.6)
    doc.save(f)
    monkeypatch.setattr("stoa.data.abspath", lambda p: p)
    a = C.count_like(SimpleNamespace(user_scale=None, px_per_m=None), str(f), "How many like this?",
                     C.Ask(point=(100, 150), space="pdf_pt"))
    assert a is not None and a.value == 5, (a.value if a else None, a.trace if a else None)
    assert all(e.bbox_space == "pdf_pt" for e in a.evidence)


def test_room_gloss_and_waschkueche():
    """F4 20:16: 'pesulan' (laundry) was machine-translated as 'washbasin'; the glossary quotes the English name first."""
    from stoa.pipeline import _room_gloss
    from stoa.router.intent import parse

    assert _room_gloss("Mikä on pesulan pinta-ala?") == ('Mikä on "laundry" pinta-ala?', [("pesulan", "laundry")])
    assert _room_gloss("Mikä on pesualtaiden määrä?")[1] == []  # pesuallas = washbasin: not a laundry
    assert _room_gloss("Hur stor är tvättstugan?")[1] == [("tvättstugan", "laundry")]
    assert _room_gloss("What is the area of the laundry?")[1] == []
    assert parse("Wie groß ist die Waschküche?").target != "kitchen"
    assert parse("Wie groß ist die Küche?").target == "kitchen"


def test_region_left_to_f1_when_its_feature_is_on(monkeypatch):
    """F1 20:32: with STOA_FEATURES=region the region feature answers regions; the pipeline answers as asked."""
    monkeypatch.setenv("STOA_FEATURES", "batch,region")
    pipe = SimpleNamespace(user_scale=100, px_per_m=None)
    ctx = C.Ask(region=[(0, 0), (72, 0), (72, 72), (0, 72)], space="pdf_pt")
    seen = []
    a = C.answer(pipe, "x.pdf", "What is the area of Kitchen?", ctx,
                 solve=lambda p, q: seen.append(q) or Answer(value=12.5, unit="m2", confidence=0.6))
    assert a.value == 12.5 and seen == ["What is the area of Kitchen?"]
    assert any("region feature" in t for t in a.trace)


def test_gross_line_split_over_two_ocr_lines(monkeypatch):
    """X1 20:38: a scan's OCR prints 'Overali GIFA' and '1274m2' as two lines; the number is still the gross total."""
    import stoa.solvers.label_count as LC
    from stoa.pipeline import _gross_line

    lines = [("Overali GIFA", (309, 3091, 490, 3133)), ("1274m2", (435, 3216, 527, 3247)),
             ("Total:", (301, 3237, 372, 3275)), ("TARGET: 4844m²", (301, 3293, 505, 3331)),
             ("Sports Hall", (900, 900, 1000, 930)), ("74.5m2", (900, 940, 980, 970))]
    monkeypatch.setattr(LC, "_image_lines", lambda p: lines)
    monkeypatch.setattr("stoa.data.abspath", lambda p: p)
    assert _gross_line("x.png", 1274, "How big is the Sports Hall?") == "Overali GIFA 1274m2"
    assert _gross_line("x.png", 4844, "What is the area of Resistant Materials?") == "TARGET: 4844m²"
    assert _gross_line("x.png", 74.5, "How big is the Sports Hall?") is None
