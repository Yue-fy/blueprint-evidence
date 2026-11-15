"""E1 cycle 17: lineplan answers the area printed with the matched label when its trace disagrees (X1 02:24)."""


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
def test_lineplan_area_printed_with_the_label_beats_a_disagreeing_trace(tmp_path, monkeypatch):
    """X1 02:24: a room tag that prints its area ('COMMS ROOM' / '120 SF') beats a traced outline that disagrees with it
    by > 20 % (a pocket / a leak): the printed number is the answer, its text the exact evidence; a trace within 20 %
    of it stays the answer; E1_PRINTED_TAG=0 restores the trace."""
    import pymupdf

    from stoa.evidence import lineplan as LP
    from stoa.evidence.linework import PT_TO_M_AT_1

    k = 100 * PT_TO_M_AT_1
    traced = 100 * 150 * k * k  # room 2 of _line_only_sheet: 18.67 m²
    for tag, want_printed in (("120 SF", True), ("200 SF", False)):
        f = str(tmp_path / f"tag{tag[:3]}.pdf")
        _line_only_sheet(f)
        doc = pymupdf.open(f)
        doc[0].insert_text((335, 184), tag, fontsize=6)
        g = f.replace(".pdf", "_t.pdf")
        doc.save(g)
        a = LP.answer(g, "How big is the comms room?")
        assert a is not None and a.unit == "m2", (tag, a)
        if want_printed:
            assert abs(a.value - 120 * 0.09290304) <= 0.01 and a.evidence[0].exact is True, a
            assert "printed" in a.evidence[0].note and a.confidence <= 0.6
            assert any("printed number wins" in s for s in a.assumptions), a.assumptions
        else:
            assert abs(a.value / traced - 1) <= 0.03 and a.evidence[0].exact is False, a
        if want_printed:
            monkeypatch.setattr(LP, "PRINTED_TAG", False)
            b = LP.answer(g, "How big is the comms room?")
            monkeypatch.setattr(LP, "PRINTED_TAG", True)
            assert b is not None and abs(b.value / traced - 1) <= 0.03, b
