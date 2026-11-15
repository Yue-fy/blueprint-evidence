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


def _schedule_set(path):
    """A 2-page set like AFCEE A-103 + A-602: a module plan whose rooms carry numbers ('7.01'), and a DOOR SCHEDULE
    whose door numbers are room number + letter with width / height columns; 7.04A (10'x10' overhead) has no symbol."""
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    _door(page, (100, 200), 0, -90)                       # 7.01A: a 0.9 m single
    h1, h2 = (300, 200), (300 + 2 * R, 200)               # 7.03A: a 1.8 m pair
    _door(page, h1, 0, -90)
    _door(page, h2, 180, 270)
    page.insert_text((110, 240), "7.01", fontsize=6)
    page.insert_text((330, 240), "7.03", fontsize=6)
    page.insert_text((480, 260), "7.04", fontsize=6)
    for i in range(30):
        page.insert_text((60 + (i % 3) * 300, 700 + (i // 3) * 8), f"note {i} see detail", fontsize=5)
    page.insert_text((60, 520), "MODULE 07 - SUPPORT - FLOOR PLAN", fontsize=12)
    page.insert_text((60, 535), "SCALE 1:100", fontsize=7)
    page.insert_text((640, 520), "MODULE 07 - SUPPORT - RCP", fontsize=12)
    page.insert_text((640, 535), "SCALE 1:100", fontsize=7)
    page.insert_text((700, 240), "7.08", fontsize=6)      # a room number in the RCP: never in scope
    sch = doc.new_page(width=1190, height=842)
    sch.insert_text((100, 80), "DOOR SCHEDULE", fontsize=10)
    for x, t in ((100, "Number"), (160, "Width"), (220, "Height"), (280, "Comments")):
        sch.insert_text((x, 110), t, fontsize=7)
    rows = [("7.01A", "3' - 0\"", "7' - 0\""), ("7.03A", "6' - 0\"", "7' - 0\""), ("7.04A", "10' - 0\"", "10' - 0\""),
            ("7.08A", "3' - 0\"", "7' - 0\""), ("8.00A", "3' - 0\"", "7' - 0\"")]
    for i, (k, w, h) in enumerate(rows):
        y = 130 + 14 * i
        sch.insert_text((100, y), k, fontsize=7)
        sch.insert_text((160, y), w, fontsize=7)
        sch.insert_text((220, y), h, fontsize=7)
    doc.save(str(path))
    return str(path)


def test_door_schedule_reconciles_the_plan(tmp_path):
    """Captain 21:13 / X1 21:22: plan count vs the set's DOOR SCHEDULE rows of the same view (door number = room
    number + letter); a scheduled door with no symbol (overhead) is counted from its row; 'swing doors' stays arcs;
    schedule questions count rows (whole schedule / one module)."""
    from stoa.solvers.openings import _dim_m, door_schedule

    assert _dim_m("3' - 0\"") == pytest.approx(0.914, abs=0.001) and _dim_m("10'-0\"") == pytest.approx(3.048, abs=0.001)
    assert _dim_m("3' - 1 1/2\"") == pytest.approx(0.953, abs=0.001) and _dim_m("900") == 0.9 and _dim_m("abc") is None
    path = _schedule_set(tmp_path / "set.pdf")
    rows = door_schedule(path, skip_page=0)
    assert [r["key"] for r in rows] == ["7.01A", "7.03A", "7.04A", "7.08A", "8.00A"]
    assert rows[2]["width_m"] == pytest.approx(3.048, abs=0.01) and rows[2]["page"] == 1
    s = OpeningsSolver()
    a = s.solve(path, "How many doors are drawn on the module 07 support floor plan?")
    assert a.value == 3 and a.confidence >= 0.8, (a.value, a.trace)
    assert len(a.evidence) == 3 and any("7.04A" in e.note and "schedule" in e.note for e in a.evidence)
    assert any("plan 2 · schedule 3" in x for x in a.assumptions), a.assumptions
    sw = s.solve(path, "How many swing doors are drawn on the module 07 support floor plan?")
    assert sw.value == 2
    assert s.solve(path, "How many double doors are in the support module?").value == 1
    assert s.solve(path, "How many doors are listed in the door schedule?").value == 5
    m = s.solve(path, "According to the door schedule, how many doors does module 07 have?")
    assert m.value == 3 and all(e.page == 1 for e in m.evidence)
    assert s.solve(path, "How many fire rated doors does the door schedule list?").value is None
    oh = s.solve(path, "How many overhead doors does the support module have?")
    assert oh.value == 1 and "7.04A" in oh.evidence[0].note


def test_view_lists_and_convention_clauses():
    """X1 sl-252 / sl-253: scope words come from the asked clause; a list of views names each of them."""
    from stoa.solvers.openings import _scope_match

    q = parse_question("How many door leaves does the support module have? Count both leaves of a pair; an overhead "
                       "door counts as one.")
    assert q["leaves"] and "overhead" not in q["scope"] and "support" in q["scope"]
    cells = [{"text": t, "kind": "plan"} for t in ("MODULE 05 - RESTROOMS - FLOOR PLAN", "MODULE 06 - STAIRS - FLOOR PLAN",
                                                   "MODULE 07 - SUPPORT - FLOOR PLAN")]
    q2 = parse_question("How many doors are there in total in the restrooms, stairs and support modules?")
    assert len(_scope_match(q2["scope"], cells)) == 3
    assert len(_scope_match(["support", "module"], cells)) == 1
    assert _scope_match(["kitchen", "stairs"], cells) == []        # a word naming no view: not a list of views


def test_door_schedule_needs_a_door_number_column(tmp_path):
    """A-801 style ('Room Number' / 'Door Type' columns under 'Door Schedule'): no door-id column -> no rows (codes
    like 'A1' must never match a grid bubble); AW A-601 style: one merged header 'DOOR NO. TYPE', a pair listed leaf
    by leaf (101A twice), metric sizes."""
    from stoa.solvers.openings import door_schedule

    doc = pymupdf.open()
    pg = doc.new_page(width=1190, height=842)
    pg.insert_text((100, 80), "DOOR SCHEDULE", fontsize=10)
    pg.insert_text((100, 110), "Room Number", fontsize=7)
    pg.insert_text((200, 110), "Door Type", fontsize=7)
    for i, (rm, ty) in enumerate((("100", "B2"), ("100", "B2"), ("103", "A1"), ("106", "A1"))):
        pg.insert_text((100, 130 + 14 * i), rm, fontsize=7)
        pg.insert_text((200, 130 + 14 * i), ty, fontsize=7)
    pg2 = doc.new_page(width=1190, height=842)
    pg2.insert_text((100, 80), "CUT AND COVER DOOR SCHEDULE", fontsize=10)
    pg2.insert_text((100, 110), "DOOR NO. TYPE", fontsize=7)
    pg2.insert_text((200, 110), "WIDTH", fontsize=7)
    pg2.insert_text((260, 110), "HEIGHT", fontsize=7)
    pg2.insert_text((330, 110), "COMMENTS", fontsize=7)
    for i, k in enumerate(("101A", "101A", "102", "103")):
        pg2.insert_text((100, 130 + 14 * i), k, fontsize=7)
        pg2.insert_text((200, 130 + 14 * i), "915", fontsize=7)
        pg2.insert_text((260, 130 + 14 * i), "2100", fontsize=7)
        if k == "102":
            pg2.insert_text((330, 130 + 14 * i), "CASED OPENING", fontsize=7)
        if k == "103":
            pg2.insert_text((330, 130 + 14 * i), "SLIDING", fontsize=7)
    path = tmp_path / "sched.pdf"
    doc.save(str(path))
    rows = door_schedule(str(path))
    assert [r["key"] for r in rows] == ["101A", "102", "103"], rows
    assert rows[0]["leaves"] == 2 and rows[1]["leaves"] == 1 and rows[0]["width_m"] == 0.915 and rows[0]["page"] == 1
    # X1 sl-272..276 (SFO A-303): filters written in the rows' own text; a word in no row -> abstain
    s = OpeningsSolver()
    assert s.solve(str(path), "How many cased openings (no door leaf) does the door schedule list?").value == 1
    assert s.solve(str(path), "How many sliding doors are in the door schedule?").value == 1
    assert s.solve(str(path), "How many actual doors does the door schedule list, not counting cased openings?"
                   ).value == 2
    assert s.solve(str(path), "How many doors are marked as secure doors in the schedule?").value is None


def test_mullion_lights_are_one_window(tmp_path):
    """S0 21:33 (d28 sealed, a room's windows 2 -> 4) / EFA MFL 3: two lights 5 cm apart (a mullion) = ONE window; a
    solid pier (0.6 m) separates windows. A room takes only units lying ALONG its edges."""
    from stoa.solvers.openings import _unit_on_edge, vector_windows

    m = 1000 / 200 / 25.4 * 72  # 1:200
    doc = pymupdf.open()
    page = doc.new_page(width=1190, height=842)
    # 2 windows of 2 lights each (0.95 + 1.5 m, mullion 0.05 m), a 0.6 m pier between the windows
    w1 = [(200, 200 + 0.95 * m), (200 + 1.0 * m, 200 + 2.5 * m)]
    w2 = [(200 + 3.1 * m, 200 + 4.05 * m), (200 + 4.1 * m, 200 + 5.6 * m)]
    _wall_with_windows(page, 100, 190, w1 + w2, m)
    path = tmp_path / "mullion.pdf"
    doc.save(path)
    units = vector_windows(str(path), 0, 200)
    assert len(units) == 2 and all(u.get("lights") == 2 for u in units), units
    import os as _os
    _os.environ["STOA_DR_MULLION"] = "0"
    try:
        assert len(vector_windows(str(path), 0, 200)) == 4
    finally:
        _os.environ.pop("STOA_DR_MULLION", None)
    room = [(0, 0), (100, 0), (100, 50), (0, 50)]
    assert _unit_on_edge(room, [20, -1, 60, 1], 5)          # along the top edge
    assert not _unit_on_edge(room, [99, -20, 101, 10], 5)   # perpendicular, mostly beyond the corner
    assert not _unit_on_edge(room, [96, -1, 140, 1], 5)     # the next room's window on the same wall line


def test_exclusion_is_all_minus_part():
    """X1 sl-268 (Ziying 104 = 115 - 11): 'if WC and shower cubicle doors are not counted' was answered with the
    cubicle doors themselves (10 @0.7). Only the asked clause is split (CubiCasa's rule sentence is not)."""
    from stoa.solvers.openings import split_exclusion

    assert split_exclusion("How many doors are there on the whole plan if WC and shower cubicle doors are not "
                           "counted?") == ("How many doors are there on the whole plan?",
                                           "How many WC and shower cubicle doors are drawn?")
    assert split_exclusion("How many doors are on the plan, excluding the cupboards?") == (
        "How many doors are on the plan?", "How many cupboards doors are drawn?")
    assert split_exclusion("How many doors are shown on this floor plan? Count doorways fitted with a door; open "
                           "doorways without a door leaf do not count.") is None
    assert split_exclusion("How many doors does the kitchen have?") is None


def test_wing_question_and_t_plan_arms():
    """H 22:24 (AFCEC dorm A-2, 'How many doors are in the north wing?'): the wing = the footprint's arm on that side,
    cut where it meets the wider core; a plan with no arm on the asked side has no such wing."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from stoa.solvers.openings import footprint_mask, wing_arm

    q = parse_question("How many doors are in the north wing?")
    assert q["wing"] == "north" and q["scope"] == []
    assert parse_question("How many windows does the western wing have?")["wing"] == "west"
    assert parse_question("How many doors are on the west facade of the classroom wing?")["wing"] is None
    # a T: north arm 160 wide (x 700-860, y 200-640) + a narrow link (x 770-800) + core (x 770-970, y 760-1080) +
    # west / east arms (y 915-1070)
    g = np.full((1300, 2000), 255, np.uint8)
    for x0, y0, x1, y1 in ((700, 200, 860, 640), (770, 640, 800, 760), (770, 760, 970, 1080), (400, 915, 1360, 1070)):
        g[y0:y1, x0:x1] = 200
        g[y0:y0 + 3, x0:x1] = 0
        g[y1 - 3:y1, x0:x1] = 0
    g[50:53, 50:1950] = 0  # a sheet frame line + a legend swatch: not the building
    g[100:115, 1500:1560] = 120
    F = footprint_mask(g)
    assert F is not None and F[500, 780] and not F[107, 1530]
    A, info = wing_arm(F, "top")
    assert A[300, 780] and A[700, 785] and not A[900, 870]  # arm + link, not the core
    assert 0.1 < info["frac"] < 0.4 and info["neck"]
    A, info = wing_arm(F, "left")
    assert A[1000, 450] and not A[1000, 900]
    A, info = wing_arm(F, "bottom")
    assert A is None or info["frac"] > 0.7  # no south wing


def test_exploded_arc_is_not_a_rounded_corner(tmp_path):
    """AFCEC A-2 draws each swing as 4 loose ~2 pt segments: its own end segments are not lines 'running on' along the
    tangent (the rounded-box rule dropped all 201 swings and counted 81 chair arcs instead)."""
    path = tmp_path / "exploded.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for i in range(24):  # the page-level switch needs a population (>= 20 arcs the corner rule would drop)
        hinge = (60 + 120 * (i % 6), 120 + 110 * (i // 6))
        pts = _arc_pts(hinge, R, 0, -90, n=4)  # 4 coarse segments, 22.5 degrees each
        sh = page.new_shape()
        for p, q in zip(pts, pts[1:]):
            sh.draw_line(p, q)
            sh.finish(color=(0, 0, 0), width=0.25, closePath=False)
        sh.commit()
    doc.save(path)
    sw = find_swings(str(path), 0, 100.0)
    assert len(sw["arcs"]) + len(sw.get("leafless") or []) == 24


def test_closet_doors_by_the_space_behind():
    """captain 23:04 (Ziying, AFCEC north wing 34 = 16 x (entry + bathroom) + 2): a door whose one side is a small EMPTY
    enclosed space is a closet door (not counted); a bathroom pocket wraps round fixtures; a room is big."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from stoa.solvers.openings import closet_doors

    ppm = 50.0
    g = np.full((400, 600), 218, np.uint8)  # light module fill
    g[:, 0:4] = g[:, -4:] = g[0:4, :] = g[-4:, :] = 0
    g[100:104, :] = 0  # wall between the closet / bathroom row (y < 100) and the room (y > 104)
    g[0:100, 80:84] = 0  # closet x 4-80 (1.5 m x 1.9 m = 2.9 m² box... make it smaller below)
    g[60:64, 4:80] = 0  # closet = x 4..80, y 64..100: 1.5 x 0.7 m
    g[0:100, 300:304] = 0  # bathroom x 84..300, with a fixture (toilet) block inside
    g[30:70, 150:230] = 0
    g[45:55, 160:220] = 218  # fixture outline only
    closet_box = [30, 92, 60, 112]  # door in the wall at y 100-104 between closet and room
    bath_box = [100, 92, 130, 112]
    room_box = [400, 380, 440, 400]  # a door in the outer wall
    fl = closet_doors(g, [closet_box, bath_box, room_box], ppm)
    assert fl == [True, False, False]


def _module_corridor(np, rooms=6):
    """6 identical rooms (5 x 5 m, a 2 x 2 m bathroom in one corner) on a 3 m corridor, 20 px/m; per room an entry door
    (corridor wall) and a bathroom door; one stair door at the corridor's end."""
    g = np.full((180, 700), 255, np.uint8)
    g[:, 0:4] = g[:, -4:] = g[0:4, :] = g[-4:, :] = 0
    g[104:108, :] = 0  # corridor wall
    boxes = []
    for i in range(rooms):
        r0 = 4 + 100 * i
        g[0:104, r0 + 96:r0 + 100] = 0  # party wall
        g[4:48, r0 + 44:r0 + 48] = 0  # bathroom walls
        g[44:48, r0:r0 + 48] = 0
        g[60:70, r0 + 60:r0 + 80] = 0  # a bed (furniture splits nothing here)
        boxes += [[r0 + 70, 96, r0 + 90, 116], [r0 + 38, 20, r0 + 54, 36]]
    boxes.append([686, 120, 700, 150])
    return g, boxes


def test_repeated_room_modules():
    """captain 23:04 (Ziying's tip 3): identical rooms -> 'N rooms x k doors + other = total'; a room showing fewer
    doors than its copies is named (a missed symbol?)."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from stoa.solvers.openings import fix_notes, repeated_modules, repetition_line, settle_modules

    g, boxes = _module_corridor(np)
    rep = repeated_modules(g, boxes, [True] * len(boxes), 20.0)
    assert rep and rep["n"] == 6 and rep["k"] == 2 and rep["dev"] == [] and rep["other"] == 1
    line, ok = repetition_line(rep, 13, None, "north")
    assert ok and "6 repeated room modules" in line and "x 2 doors = 12" in line and "= 13" in line
    counted = [True] * len(boxes)
    counted[3] = False  # room 2's bathroom door not counted
    rep = repeated_modules(g.copy(), boxes, counted, 20.0)
    assert rep["dev"] == [(2, 1)] and rep["own"][3] is None
    line, ok = repetition_line(rep, 12, None, "north")
    assert not ok and "module 2 shows 1" in line and "(13 if that module has 2 like the other 5: check it)" in line
    # the left-out door sits where every other room has its bathroom door: put back (AFCEC west wing PDF: 2 entry
    # doors taken for closet doors, 32 -> 34)
    assert rep["restore"] == [3]
    fl, rep2, fix = settle_modules(g.copy(), boxes, [not c for c in counted], 20.0)
    assert fl[3] is False and fix["restored"] == [(3, 2)] and rep2["dev"] == []
    assert "counted after all (room module 2)" in fix_notes(fix, 6)[0]
    # an armchair boxed as a door in room 3 (AFCEC east wing PNG): one door too many, at a spot no copy has a door
    g8, b8 = _module_corridor(np, rooms=6)
    b8.append([4 + 200 + 15, 60, 4 + 200 + 35, 80])
    rep = repeated_modules(g8, b8, [True] * len(b8), 20.0)
    assert rep["dev"] == [(3, 3)] and rep["odd"] == [len(b8) - 1]
    # fewer than 4 alike rooms: no repetition claimed
    g3, b3 = _module_corridor(np, rooms=3)
    assert repeated_modules(g3, b3, [True] * len(b3), 20.0) is None


def test_printed_room_count_and_ocr_tiles(monkeypatch):
    """AFCEC A-2's ROOM COUNT table ('SECOND FLOOR = 48 DORMS') x the wing's share of the dormitory area; an image's
    OCR tiles that cut a word are joined on their overlap."""
    import types

    import stoa.sheet.sets as sets
    import stoa.solvers.openings as op

    rows = ["ROOM COUNT", "FIRST FLOOR = 48 DORMS", "SECOND FLOOR = 48 DORMS", "TOTAL = 144 DORMS",
            "WEST WING DORMITORY = 6,095 SF", "EAST WING DORMITORY = 6,092 SF", "NORTH WING DORMITORY = 6,285 SF",
            "NORTH WING CIRCULATION = 1,403 SF", "SECOND FLOOR PLAN"]
    monkeypatch.setattr(op, "_text_rows", lambda path, page=0: rows)
    p = op.printed_room_count("x.pdf", 0, "north")
    assert p["count"] == 48 and p["floor"] == "SECOND" and abs(p["expected"] - 16.33) < 0.05
    monkeypatch.setattr(op, "_text_rows", lambda path, page=0: rows[:4] + ["WEST WING D", "EAST WING C", "NORTH WING"])
    p = op.printed_room_count("x.png", 0, "north")
    assert p["floor"] == "every" and p["expected"] == 16.0 and "3 wings" in p["how"]
    monkeypatch.setattr(op, "_text_rows", lambda path, page=0: rows[:4])
    assert op.printed_room_count("x.png", 0, "north") is None  # no wings named: nothing to share out
    monkeypatch.undo()
    It = types.SimpleNamespace
    layer = types.SimpleNamespace(items=[It(text="SECOND FLO", box=(7779, 1234, 8187, 1306)),
                                         It(text="FLOOR =", box=(8052, 1238, 8351, 1308)),
                                         It(text="48 DORMS", box=(8496, 1238, 8830, 1308)),
                                         It(text="WEST WING D", box=(7779, 1731, 8153, 1800)),
                                         It(text="ING DORMITORY =", box=(8052, 1729, 8618, 1801))])
    monkeypatch.setattr(sets, "_cached_layer", lambda p: layer)
    assert op._text_rows("x.png") == ["SECOND FLOOR = 48 DORMS", "WEST WING DORMITORY ="]


def test_fine_sill_line_does_not_close_a_window(tmp_path):
    """F2 23:58 (DfE SEN L01 48 -> 15): a FINE line running along the facade through the window units (sill / outline,
    0.24 pt) does not close the opening; a wall-pen line (> 0.36 pt) running through still does."""
    from stoa.solvers.openings import vector_windows

    m = 1000 / 200 / 25.4 * 72  # 1:200
    spans = [(200 + k * 2.0 * m, 200 + k * 2.0 * m + 1.35 * m) for k in range(4)]
    for pen, want in ((0.24, 4), (0.7, 0)):
        doc = pymupdf.open()
        page = doc.new_page(width=1190, height=842)
        _wall_with_windows(page, 100, 190, spans, m)
        sh = page.new_shape()
        sh.draw_line((100, 150), (100, 150 + 12 * m))  # the outer face line runs on through every unit
        sh.finish(color=(0, 0, 0), width=pen)
        sh.commit()
        path = tmp_path / f"sill{pen}.pdf"
        doc.save(path)
        assert len(vector_windows(str(path), 0, 200)) == want, pen


def test_cut_line_double_door_leaf():
    """AFCEC side wings: a double-egress pair in the corridor's end wall. The core-side leaf (outside the wing) counts
    as the wing's boundary door when the leaf it touches inside the wing on the cut line was not counted (closet test /
    cupboard leaf in the same box); not when that leaf is counted already (north wing link)."""
    from stoa.solvers.openings import cut_line_leaves

    ppm = 50.0
    inner = [100, 100, 140, 140]  # leaf inside the wing, on the cut line, taken by the closet test
    outer = [141, 130, 180, 175]  # the other leaf, swinging into the core (gap 0 px)
    far = [400, 100, 440, 140]  # a core door far from the line
    on_edge = lambda b: b is inner  # noqa: E731
    assert cut_line_leaves([outer, far], [inner], [], on_edge, ppm) == [outer]
    assert cut_line_leaves([outer, far], [inner], [inner], on_edge, ppm) == []  # already counted by its inner leaf
    assert cut_line_leaves([[160, 100, 200, 140]], [inner], [], on_edge, ppm) == []  # 20 px = 0.4 m apart: no
    two = [[141, 100, 180, 128], [141, 129, 180, 170]]  # two outer leaves touching each other = one door
    assert len(cut_line_leaves(two, [inner], [], on_edge, ppm)) == 1


def test_scattered_repetition_caps_confidence():
    """H 00:4x: on the gates' detector v2 the AFCEC wings' identical rooms disagree (8 of 16 off the usual 3) -> the
    line that caps the confidence; a few odd rooms (<= 1/4) do not."""
    from stoa.solvers.openings import rep_scattered

    rep = {"n": 16, "k": 3, "dev": [(i, 2) for i in range(8)]}
    assert "8 of the 16 identical rooms" in rep_scattered(rep)
    assert rep_scattered(dict(rep, dev=[(1, 1), (2, 3), (3, 1), (4, 1)])) is None
    assert rep_scattered(None) is None


def test_exterior_units_drops_interior_partitions(tmp_path):
    """AFCEC A-2: thin double-line partitions deep inside the building read as 'window units' (289 vs ~40 on the outer
    walls). Units more than 1.2 m deeper than the outer wall ring are interior; the outline must hold the doors."""
    import pymupdf

    from stoa.solvers.openings import exterior_units

    pdf = tmp_path / "bldg.pdf"
    doc = pymupdf.open()
    pg = doc.new_page(width=842, height=595)
    pg.draw_rect(pymupdf.Rect(100, 100, 700, 450), color=(0, 0, 0), fill=(0.8, 0.8, 0.8), width=2)
    doc.save(pdf)
    doc.close()
    scale_n = 100.0  # 1 m = 28.3 pt
    arcs = [{"c": (150 + 50 * i, 300)} for i in range(10)]  # door swings inside the building
    outer = [{"box": (200 + 60 * i, 98, 230 + 60 * i, 104)} for i in range(4)]  # in the outer wall (top)
    inner = [{"box": (200 + 30 * i, 270, 220 + 30 * i, 275)} for i in range(14)]  # ~6 m inside
    kept, drop = exterior_units(str(pdf), 0, outer + inner, arcs, scale_n)
    assert len(kept) == 4 and len(drop) == 14
    # doors outside the outline (another view / a second building): the outline is not trusted
    assert exterior_units(str(pdf), 0, outer + inner, [{"c": (780, 520)}] * 10, scale_n) is None


def test_full_res_original_both_conventions(tmp_path):
    """Counting reads the full-resolution original of a shrunk picture: eval / CLI '<stem>.s6000.png' next to
    '<stem>.png', and the web upload shrunk in place with the original kept as '<stem>.full.png'."""
    from PIL import Image

    from stoa.solvers.openings import full_res

    Image.new("L", (300, 200), 255).save(tmp_path / "a.png")
    Image.new("L", (150, 100), 255).save(tmp_path / "a.s150.png")
    assert full_res(str(tmp_path / "a.s150.png")) == (str(tmp_path / "a.png"), 2.0)
    Image.new("L", (150, 100), 255).save(tmp_path / "b.png")
    Image.new("L", (450, 300), 255).save(tmp_path / "b.full.png")
    assert full_res(str(tmp_path / "b.png")) == (str(tmp_path / "b.full.png"), 3.0)
    Image.new("L", (450, 100), 255).save(tmp_path / "c.png")
    Image.new("L", (150, 100), 255).save(tmp_path / "c.s150.png")  # other aspect: not its original
    assert full_res(str(tmp_path / "c.s150.png")) == (str(tmp_path / "c.s150.png"), 1.0)
    assert full_res(str(tmp_path / "a.png")) == (str(tmp_path / "a.png"), 1.0)


def test_whole_plan_detects_on_the_original_only_with_a_gpu_detector(tmp_path, monkeypatch):
    """A whole-plan pass 2 on a shrunk picture reads the full-res original when the copy draws doors below 46 px/m
    (Stoa's AFCEC A-2 upload: 177 boxes on the copy, 213 on the original) - only when the detector service offloads
    to a GPU ('remote' in /health): on the CPU service the original would not come back within the question's budget."""
    from PIL import Image

    from stoa.solvers import openings as O

    Image.new("L", (150, 100), 255).save(tmp_path / "b.png")
    Image.new("L", (255, 170), 255).save(tmp_path / "b.full.png")
    img = str(tmp_path / "b.png")
    monkeypatch.delenv("STOA_DR_FULLRES_DET", raising=False)
    monkeypatch.setattr(O, "_det_health", lambda url: {"ok": True, "remote": "http://127.0.0.1:1"})
    assert O._fullres_for_det(img, 35.0) == (str(tmp_path / "b.full.png"), 1.7)
    assert O._fullres_for_det(img, 51.0) == (img, 1.0)  # DfE copy at ~51 px/m: the copy is fine
    monkeypatch.setattr(O, "_det_health", lambda url: {"ok": True})  # CPU service (the demo's :8143 tonight)
    assert O._fullres_for_det(img, 35.0) == (img, 1.0)
    monkeypatch.setenv("STOA_DR_FULLRES_DET", "1")
    assert O._fullres_for_det(img, 35.0)[1] == 1.7
    monkeypatch.setenv("STOA_DR_FULLRES_DET", "0")
    assert O._fullres_for_det(img, 35.0) == (img, 1.0)


def test_closet_door_questions(tmp_path, monkeypatch):
    """Our door count leaves closet / wardrobe doors out and says how many: 'how many closet doors?' counts THOSE
    ('only'), '... including closets?' counts both ('with'), '... excluding closets?' is the plain count (not all minus
    closets, which would take them out twice). A vector PDF does not count closet doors alone yet: abstain."""
    from stoa.contract import Answer
    from stoa.solvers.openings import OpeningsSolver, parse_question

    assert parse_question("How many closet doors are in the north wing?")["closets"] == "only"
    assert parse_question("How many doors are in the north wing, including closets?")["closets"] == "with"
    assert parse_question("How many doors are on this floor including the wardrobe doors?")["closets"] == "with"
    assert parse_question("How many doors are in the north wing?")["closets"] is None
    assert parse_question("How many doors does the closet have?")["closets"] is None  # a room, not a door kind
    assert parse_question("How many cupboard doors are there?")["scope"] == []
    seen = []

    def fake_image(self, img, path, question, q):
        seen.append(q)
        return Answer(value=34, unit="count", confidence=0.7, assumptions=["Repetition check"])

    monkeypatch.setattr(OpeningsSolver, "_image", fake_image)
    png = str(tmp_path / "plan.png")
    a = OpeningsSolver().solve(png, "How many doors are in the east wing, excluding closets?")
    assert a.value == 34 and seen[-1]["closets"] is None and seen[-1]["wing"] == "east"
    assert a.assumptions[0].startswith("closet / wardrobe / cupboard doors are left out")
    a = OpeningsSolver().solve(str(tmp_path / "plan.pdf"), "How many closet doors are in the north wing?")
    assert a.value is None and "vector PDF" in a.trace[-1]


def test_each_room_and_exterior_on_pictures(tmp_path):
    """'How many doors does each dorm room have?' is a per-room count (the repetition check's doors per identical
    room), not a scope; a vector PDF does not read it yet (it would count the whole sheet). Exterior doors are not told
    apart on a picture (the picture path counted every door: AFCEC 113 @0.45)."""
    from stoa.solvers.openings import OpeningsSolver, parse_question

    q = parse_question("How many doors does each dorm room have?")
    assert q["each"] == "dorm room" and q["scope"] == []
    assert parse_question("How many doors does each of the dorm rooms have?")["each"] == "dorm room"
    assert parse_question("How many doors does room 101 have?")["each"] is None
    assert parse_question("How many doors are there on one floor?")["each"] is None
    a = OpeningsSolver().solve(str(tmp_path / "plan.pdf"), "How many doors does each dorm room have?")
    assert a.value is None and "per room" in a.trace[-1]
    a = OpeningsSolver().solve(str(tmp_path / "plan.png"), "How many exterior doors does the building have?")
    assert a.value is None and "picture" in a.trace[-1]
