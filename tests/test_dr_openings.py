"""DR: door / window counts from vector door swings (stoa/solvers/openings.py) on a synthetic CAD sheet."""

from __future__ import annotations

import math

import pytest

pymupdf = pytest.importorskip("pymupdf")

from stoa.solvers.openings import OpeningsSolver, find_swings, group_doors, parse_question  # noqa: E402

PT_PER_M = 1000 / 100 / 25.4 * 72  # 1:100 -> points per metre of building (28.35)
R = 0.9 * PT_PER_M  # a 0.9 m leaf


def _arc_pts(c, r, a0, a1, n=12):
    return [(c[0] + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             c[1] + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def _door(page, hinge, a0, a1, exploded=False):
    """Leaf line from the hinge to the arc's end at a1, arc from a0 (closed, on the wall) to a1 (open)."""
    pts = _arc_pts(hinge, R, a0, a1)
    sh = page.new_shape()
    if exploded:  # every arc segment its own path (USACE A-101F)
        for p, q in zip(pts, pts[1:]):
            sh.draw_line(p, q)
            sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
    else:
        sh.draw_polyline(pts)
        sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
    sh.draw_line(hinge, pts[-1])
    sh.finish(color=(0, 0, 0), width=0.5, closePath=False)
    sh.commit()


@pytest.fixture(scope="module")
def sheet(tmp_path_factory):
    path = tmp_path_factory.mktemp("dr") / "sheet.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    # view 1 (left column): a single door, an exploded single door, a double door; a grid bubble and a rounded box
    _door(page, (100, 200), 0, -90)
    _door(page, (200, 200), 0, -90, exploded=True)
    h1, h2 = (300, 200), (300 + 2 * R, 200)
    _door(page, h1, 0, -90)  # closed position points at the other hinge
    _door(page, h2, 180, 270)
    sh = page.new_shape()
    sh.draw_circle((150, 400), R)  # grid bubble with a door-like radius: 4 quarter arcs
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.draw_rect(pymupdf.Rect(250, 350, 400, 450), radius=R / 150)  # rounded corners of a door's radius
    sh.finish(color=(0, 0, 0), width=0.25)
    sh.commit()
    # view 2 (right column): a reflected ceiling plan with a door drawn in it (must not be counted)
    _door(page, (700, 200), 0, -90)
    for i in range(30):  # small notes so the view titles are the big type
        page.insert_text((60 + (i % 3) * 300, 700 + (i // 3) * 8), f"note {i} see detail", fontsize=5)
    page.insert_text((60, 520), "GROUND FLOOR PLAN", fontsize=12)
    page.insert_text((60, 535), "SCALE 1:100", fontsize=7)
    page.insert_text((640, 520), "GROUND FLOOR - REFLECTED CEILING PLAN", fontsize=12)
    page.insert_text((640, 535), "SCALE 1:100", fontsize=7)
    doc.save(str(path))
    return str(path)


def test_parse_question():
    q = parse_question("How many doors does the administration module (module 02) have?")
    assert q["target"] == "door" and "02" in q["scope"] and "administration" in q["scope"]
    assert parse_question("How many of the filter building's doors are double doors?")["double"]
    assert parse_question("How many windows does English 1 have?")["target"] == "window"
    assert parse_question("How many door leaves are there?")["leaves"]
    assert parse_question("How many doors and windows are there?") is None  # a list: compose splits it
    assert parse_question("How many metres of skirting, ignoring door openings?") is None
    assert parse_question("What is the width of the main door?") is None


def test_swings_pairs_and_rejects(sheet):
    sw = find_swings(sheet, 0, 100.0)
    # 4 leaves in view 1 + 1 in the RCP; the circle and the rounded corners are not doors
    assert len(sw["arcs"]) == 5, [round(a["r"], 1) for a in sw["arcs"]]
    doors = group_doors(sw["arcs"])
    assert sorted(d["leaves"] for d in doors) == [1, 1, 1, 2]


def test_count_plan_views_only(sheet):
    a = OpeningsSolver().solve(sheet, "How many doors are on this plan?")
    assert a.value == 3, a.trace  # single + exploded + one double; the RCP door is left out
    assert len([e for e in a.evidence if e.bbox_space == "pdf_pt"]) == 3
    assert any("ONE door with 2 leaves" in s for s in a.assumptions)
    assert OpeningsSolver().solve(sheet, "How many door leaves are on this plan?").value == 4
    assert OpeningsSolver().solve(sheet, "How many double doors are there?").value == 1


def test_scope_needs_a_word():
    from stoa.solvers.openings import _scope_match

    cells = [{"text": "Secondary Type 3 600 First Floor", "kind": "plan"}]
    assert _scope_match(["english", "3", "together"], cells) == []
    two = [{"text": "MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"}, {"text": "MODULE 02-ADMINISTRATION - FLOOR PLAN"}]
    assert [c["text"] for c in _scope_match(["module", "02"], two)] == [two[1]["text"]]
    assert [c["text"] for c in _scope_match(["entry", "vestibule", "module"], two)] == [two[0]["text"]]


def test_merge_ocr_title_pieces():
    from stoa.solvers.openings import _merge_line_items

    items = [("1", [1379, 3387, 1448, 3462]), ("MODULE 01- ENTRY VESTIE", [1479, 3375, 2147, 3423]),
             ("VESTIBULE - FLOOR PLAN", [2013, 3375, 2636, 3422]), ('1/8" = 1\'-0"', [1491, 3433, 1740, 3482])]
    lines = [t for t, _ in _merge_line_items(items)]
    assert any("ENTRY" in t and "FLOOR PLAN" in t for t in lines), lines


def test_pick_pages_by_storey(tmp_path):
    from stoa.solvers.openings import pick_pages

    doc = pymupdf.open()
    for name in ("Ground Floor", "First Floor", "Second Floor"):
        pg = doc.new_page(width=595, height=420)
        for i in range(20):
            pg.insert_text((20, 20 + 8 * i), f"note {i}", fontsize=5)
        pg.insert_text((300, 400), f"Secondary School {name}", fontsize=14)
    p = tmp_path / "set.pdf"
    doc.save(str(p))
    assert pick_pages(str(p), "How many doors are on the first floor?")[0] == [1]
    assert pick_pages(str(p), "How many doors are in the building?")[0] == [0, 1, 2]


def test_exterior_parsed_but_off():
    q = parse_question("How many exterior doors does the filter building have?")
    assert q["side"] == "exterior" and "exterior" not in q["scope"] and "filter" in q["scope"]
    assert parse_question("How many doors (interior doors and entrance doors together) are there?")["side"] is None


def test_swiss_wording_has_no_scope():
    q = parse_question("How many doors (interior doors and entrance doors together) are drawn on this floor plan?")
    assert q["side"] is None and q["scope"] == []


def test_detector_budget_and_busy_service(monkeypatch):
    """S0 19:45: one detector budget per question; a busy service is not asked at all."""
    from stoa.solvers import openings as O

    monkeypatch.setenv("STOA_DR_DET_TIMEOUT", "0")
    O._det_budget_start()
    assert O._det_client("x.png", 1.0, 0.4) is None and "budget" in O._det_why()
    monkeypatch.setenv("STOA_DR_DET_TIMEOUT", "25")
    O._det_budget_start()
    monkeypatch.setattr(O, "_det_health", lambda url: {"inflight": 27})  # an old service without max_queue
    assert O._det_client("x.png", 1.0, 0.4) is None and "busy (27" in O._det_why()
    monkeypatch.setattr(O, "_det_health", lambda url: None)
    assert O._det_client("x.png", 1.0, 0.4) is None and "down" in O._det_why()
    monkeypatch.setattr(O, "_det_health", lambda url: {"inflight": 5, "max_queue": 4})
    assert O._det_busy("u") == 5
    monkeypatch.setattr(O, "_det_health", lambda url: {"inflight": 2, "max_queue": 4})
    assert O._det_busy("u") == 0


def test_traced_copy_only_where_the_detector_cannot_answer(monkeypatch):
    """Image door counts read the traced vector copy for rooms on clean renders; whole-plan counts and scans stay
    with V1's detector while it is up (an openings answer makes the pipeline skip door_detect)."""
    from stoa.solvers import openings as O

    s = O.OpeningsSolver()
    monkeypatch.setattr(O, "_det_busy", lambda url: 0)
    O._det_budget_start()
    for clean, question in ((True, "How many doors are on this plan?"), (False, "How many doors does the Kitchen have?")):
        monkeypatch.setattr(O.OpeningsSolver, "_clean", staticmethod(lambda img, c=clean: c))
        tr: list = []
        assert s._via_trace("x.png", "x.png", question, O.parse_question(question), [], tr) is None
        assert "left to the door detector" in tr[-1]
    qw = O.parse_question("How many windows does the Kitchen have?")
    assert s._via_trace("x.png", "x.png", "How many windows does the Kitchen have?", qw, [], []) is None


def test_windows_profile_on_filled_walls(tmp_path):
    """Wall profile (cross-check for the window detector): a room with filled walls; its top wall holds two glazed
    units (3 lines across the band) split by a solid pier; a hollow line-pair sheet is refused (None)."""
    from stoa.solvers.openings import windows_profile

    m = PT_PER_M
    path = tmp_path / "win.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    x0, y0, x1, y1, t = 100.0, 100.0, 100 + 6 * m, 100 + 5 * m, 0.3 * m  # room interior; walls 0.3 m outside it
    sh = page.new_shape()
    for r in ((x0 - t, y1, x1 + t, y1 + t), (x0 - t, y0 - t, x0, y1 + t), (x1, y0 - t, x1 + t, y1 + t),
              (x0 - t, y0 - t, x0 + 0.5 * m, y0), (x0 + 2.5 * m, y0 - t, x0 + 3.5 * m, y0),
              (x0 + 5.5 * m, y0 - t, x1 + t, y0)):  # bottom, left, right, top pieces: two 2 m gaps, a 1 m pier
        sh.draw_rect(pymupdf.Rect(*r))
    sh.finish(color=None, fill=(0, 0, 0))
    for gx in ((x0 + 0.5 * m, x0 + 2.5 * m), (x0 + 3.5 * m, x0 + 5.5 * m)):
        for f in (0.0, 0.5, 1.0):  # glass + frame lines across the band
            y = y0 - f * t
            sh.draw_line((gx[0], y), (gx[1], y))
    sh.finish(color=(0, 0, 0), width=0.3)
    sh.commit()
    doc.save(str(path))
    room = [{"label": "Room", "polygon": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]}]
    r = windows_profile(str(path), 0, room, 100.0)
    assert r is not None and len(r["units"]) == 2
    assert all(1.6 <= u["len_m"] <= 2.1 for u in r["units"])
    # hollow walls: two thin lines everywhere -> refused
    path2 = tmp_path / "hollow.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    sh = page.new_shape()
    for d in (0.0, t):
        sh.draw_rect(pymupdf.Rect(x0 - d, y0 - d, x1 + d, y1 + d))
    sh.finish(color=(0, 0, 0), width=0.3)
    sh.commit()
    doc.save(str(path2))
    assert windows_profile(str(path2), 0, room, 100.0) is None


def test_door_and_window_types_abstain(sheet):
    """'sliding doors' / 'garage doors' / 'casement windows' are types a swing or window symbol does not show:
    abstain (a room called 'Garage' must not turn 'garage doors' into that room's swing doors)."""
    s = OpeningsSolver()
    for q in ("How many sliding doors are there?", "How many garage doors are on this plan?",
              "How many casement windows does the kitchen have?"):
        a = s.solve(str(sheet), q)
        assert a.value is None and "type" in a.trace[-1]
    assert s.solve(str(sheet), "How many doors are on the ground floor plan?").value is not None


def test_doors_and_windows_is_two_counts(monkeypatch):
    """'How many doors and windows does X have?' -> both counts in the asked order, team multi-part format 'a ; b'."""
    from stoa.contract import Answer
    from stoa.solvers import openings as O

    s = O.OpeningsSolver()
    assert s.can_handle("x.pdf", "How many doors and windows does the Kitchen have?")
    seen = []
    real = O.OpeningsSolver.solve

    def fake(self, path, q):
        if O._BOTH_Q.search(q):
            return real(self, path, q)
        seen.append(q)
        return Answer(value=4 if "doors" in q else 2, unit="count", confidence=0.7, solver="openings")

    monkeypatch.setattr(O.OpeningsSolver, "solve", fake)
    a = s.solve("x.pdf", "How many windows and doors does the Kitchen have?")
    assert a.value == "2 ; 4" and seen == ["How many windows does the Kitchen have?",
                                           "How many doors does the Kitchen have?"]


def _wall_with_windows(page, x, y0, spans, m):
    """A vertical outer wall at x (0.45 m thick, filled piers) with window units: 3 thin lines over each span."""
    t = 0.45 * m
    sh = page.new_shape()
    edges = [y0] + [v for s in spans for v in s] + [y0 + 30 * m]
    for a, b in zip(edges[::2], edges[1::2]):  # solid piers between the openings
        sh.draw_rect(pymupdf.Rect(x, a, x + t, b))
        sh.finish(color=None, fill=(0, 0, 0))
    for a, b in spans:
        for off in (0.0, t / 2, t):
            sh.draw_line((x + off, a), (x + off, b))
            sh.finish(color=(0, 0, 0), width=0.24)
    sh.commit()


def test_vector_windows_and_facade(tmp_path):
    """dfe-08 (Stoa sheet 1, Ziying 18): windows = wall openings bridged by parallel glazing lines; 'the west facade of
    the classroom wing' = the outermost wall band on the left, level with the 'Classroom' labels; stair treads (a long
    even run of lines) are not windows."""
    from stoa.solvers.openings import facade_units, vector_windows

    m = 1000 / 200 / 25.4 * 72  # 1:200
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    west = [(200 + k * 2.0 * m, 200 + k * 2.0 * m + 1.35 * m) for k in range(5)]
    _wall_with_windows(page, 100, 190, west, m)  # west wall: 5 windows
    _wall_with_windows(page, 100 + 12 * m, 190, west[:3], m)  # east wall of the wing: 3 windows
    sh = page.new_shape()
    for k in range(12):  # a stair: 12 treads 0.28 m apart, 1.2 m long
        yy = 500 + k * 0.28 * m
        sh.draw_line((400, yy), (400 + 1.2 * m, yy))
        sh.finish(color=(0, 0, 0), width=0.24)
    sh.commit()
    for k in range(3):
        page.insert_text((100 + 4 * m, 220 + k * 3 * m), "Secondary Classroom", fontsize=5)
    page.insert_text((100 + 4 * m, 400), "Staff Room", fontsize=5)
    page.insert_text((700, 780), "GROUND FLOOR PLAN  SCALE 1:200", fontsize=9)
    path = tmp_path / "facade.pdf"
    doc.save(path)
    units = vector_windows(str(path), 0, 200)
    assert len(units) == 8 and all(u["a"] == "v" and len(u["offs"]) == 3 for u in units)
    q = parse_question("How many windows are on the west facade of the classroom wing?")
    assert q["facade"] == {"side": "west", "word": "west", "part": ["classroom"]}
    labels = [(b[4], tuple(b[:4])) for b in pymupdf.open(path)[0].get_text("blocks")]
    got, _why = facade_units(units, q["facade"], labels, 200)
    assert len(got) == 5
    got, _why = facade_units(units, parse_question("How many windows are on the east side of the classroom wing?")
                             ["facade"], labels, 200)
    assert len(got) == 3
    a = OpeningsSolver().solve(str(path), "How many windows are on the west facade of the classroom wing?")
    assert a.value == 5 and len(a.evidence) == 5


def test_facade_doors_and_widths(sheet):
    """Facade questions about DOORS need window units to place the wall; with none the reader abstains (no guess).
    Door answers say what was counted and each door's opening width."""
    from stoa.solvers.openings import parse_question as pq

    assert pq("How many doors are on the west facade?")["facade"]["side"] == "west"
    a = OpeningsSolver().solve(str(sheet), "How many doors are on the west facade?")
    assert a.value is None
    a = OpeningsSolver().solve(str(sheet), "How many doors are on the ground floor plan?")
    if a.value is not None:
        assert a.assumptions[0].startswith("Counted door SETS")
        assert all("opening" in e.note for e in a.evidence)


def test_doors_without_swing_arcs(tmp_path):
    """Captain 21:11 / Ziying (DfE 115): a folding / louvred pair (two V leaves sharing an end in a wall opening), a
    store's angled leaf (nearest label 'Store'), a sliding door ('<- ->'); a corridor cupboard's leaf (nearest label a
    corridor) and a cabinet bracket are not doors."""
    from stoa.solvers.openings import leaf_doors, sliding_doors

    m = 1000 / 200 / 25.4 * 72  # 1:200
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    sh = page.new_shape()

    def wall(x0, y0, x1, y1):
        sh.draw_rect(pymupdf.Rect(x0, y0, x1, y1))
        sh.finish(color=None, fill=(0, 0, 0))

    def vleaf(p, q, apex):
        sh.draw_polyline([p, apex, ((p[0] + q[0]) / 2 + (apex[0] - p[0]) * 0.05, (p[1] + q[1]) / 2), q] if False else
                         [p, apex, q])
        sh.finish(color=(0, 0, 0), width=0.24, closePath=False)

    t = 0.3 * m
    # wall A with a folding pair (two leaves, 0.85 m each) at x 200..200+1.7 m
    x, y = 200, 200
    wall(100, y, x, y + t)
    wall(x + 1.7 * m, y, x + 4 * m, y + t)
    mid = (x + 0.85 * m, y)
    vleaf((x, y), mid, (x + 0.42 * m, y - 0.22 * m))
    vleaf(mid, (x + 1.7 * m, y), (x + 1.28 * m, y - 0.22 * m))
    # wall B: a store door (angled leaf) with 'Store' right behind it, and a cupboard leaf on a corridor wall
    y2 = 400
    wall(100, y2, 300, y2 + t)
    wall(300 + 0.85 * m, y2, 500, y2 + t)
    vleaf((300, y2), (300 + 0.85 * m, y2), (300 + 0.42 * m, y2 - 0.22 * m))
    wall(520, y2, 600, y2 + t)
    wall(600 + 0.85 * m, y2, 800, y2 + t)
    vleaf((600, y2), (600 + 0.85 * m, y2), (600 + 0.42 * m, y2 - 0.22 * m))
    # a cabinet bracket on a wall (out - along - back): not a leaf
    sh.draw_polyline([(150, y2 + t), (150, y2 + t + 0.3 * m), (150 + 0.9 * m, y2 + t + 0.3 * m), (150 + 0.9 * m, y2 + t)])
    sh.finish(color=(0, 0, 0), width=0.24, closePath=False)
    # sliding door arrows '<- ->' in an opening of wall C
    y3 = 600
    cx = 400
    for sgn in (-1, 1):
        tail = (cx + sgn * 0.16 * m, y3)
        tip = (cx + sgn * 0.5 * m, y3)
        sh.draw_line(tail, tip)
        sh.finish(color=(0, 0, 0), width=0.24)
        for b in (-1, 1):
            sh.draw_line(tip, (tip[0] - sgn * 0.08 * m, tip[1] + b * 0.04 * m))
            sh.finish(color=(0, 0, 0), width=0.24)
    sh.commit()
    page.insert_text((300 + 0.2 * m, y2 + t + 0.9 * m), "Store", fontsize=5)
    page.insert_text((600 + 0.2 * m, y2 - 0.9 * m), "Corridor", fontsize=5)
    path = tmp_path / "leaves.pdf"
    doc.save(path)
    got = leaf_doors(str(path), 0, 200)
    kinds = sorted(d["kind"] for d in got)
    assert kinds == ["angled", "folding"], got
    assert [d for d in got if d["kind"] == "angled"][0]["room"].startswith("Store")
    sl = sliding_doors(str(path), 0, 200)
    assert len(sl) == 1 and sl[0]["kind"] == "sliding"
