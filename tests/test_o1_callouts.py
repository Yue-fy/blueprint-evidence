"""Slab callouts (stoa/solvers/raster/callouts.py): the thickness is the callout's leading number, not '@ spacing'."""

from stoa.solvers.raster import callouts as CO
from stoa.solvers.raster import textlayer as T


def _tl(items, size=(2000, 1500)):
    return T.TextLayer(path="x.png", size=size, items=[T.TextItem(t, list(map(float, b))) for t, b in items])


def _sheet():
    return _tl([("150 SLAB-ON-GRADE W", [600, 270, 700, 280]), ("D13 @ 300", [600, 282, 670, 292]),
                ("TOP OF SLAB EL. = 1000", [600, 294, 720, 304]),
                ("300 SLAB-ON-GRADE W/", [300, 310, 400, 320]), ("D16 @ 300 TOP FACE", [300, 322, 400, 332]),
                ("PIPE TRENCH, SEE", [100, 100, 200, 110])])


def test_thickness_from_the_callout_with_the_questions_bar():
    tl = _sheet()
    a = CO.thickness_answer("x.png", "How thick is the slab reinforced with D13 bars?", tl)
    assert a is not None and a.value == 150 and a.unit == "mm" and a.confidence == CO.CONF
    b = CO.thickness_answer("x.png", "How thick is the slab with D16 bars?", tl)
    assert b is not None and b.value == 300


def test_no_answer_when_ambiguous_or_not_a_slab_thickness():
    tl = _sheet()
    assert CO.thickness_answer("x.png", "How thick is the slab?", tl) is None  # 150 and 300 printed
    assert CO.thickness_answer("x.png", "How thick is the slab with D25 bars?", tl) is None  # no such block
    assert CO.thickness_answer("x.png", "What is the area of the slab with D13 bars?", tl) is None
    one = _tl([("200 THK SLAB", [10, 10, 80, 20]), ("SLAB 200 THK", [10, 400, 80, 410])])
    a = CO.thickness_answer("x.png", "How thick is the floor slab?", one)
    assert a is not None and a.value == 200 and a.confidence == CO.SAME_CONF
    inch = _tl([('6" CONC. SLAB ON GRADE', [10, 10, 120, 20]), ('#4 @ 12" O.C.', [10, 22, 90, 32])])
    a = CO.thickness_answer("x.png", "How thick is the slab with #4 bars?", inch)
    assert a is not None and a.value == 6 and a.unit == "in"


def test_listed_sum_of_note_rows_with_split_ocr_reads():
    from stoa.solvers.raster import notes as NO
    tl = _tl([("1. APPROXIMATE TRENCH LENGTHS ARE PROVIDED BELOW.", [100, 90, 500, 100]),
              ("MOTOR CONTROL CENTER TRENCH LEN", [110, 104, 300, 108]),  # clipped read, its text is right
              ("ULL LENGTH 4975", [270, 104, 360, 108]),  # clipped re-read of the next item: dropped
              ("CH LENGTH - 4275", [270, 104, 360, 114]),
              ("PUMP CONTROL PANEL TRENCH LENGTH - 1825", [110, 118, 360, 128]),
              ("PIPE TRENCH LENGTH - 1750 (SEE NOTE 3)", [110, 132, 360, 142]),
              ("2. SEE SHEET S-501 FOR PIPE TRENCH DETAIL.", [100, 146, 500, 156]),
              ("FOUNDATION AND SLAB PLAN", [900, 20, 912, 160])])  # vertical title text
    a = NO.listed_sum_answer("x.png", "What is the total length of the three trenches listed in the notes?", tl)
    assert a is not None and abs(a.value - 7.85) < 1e-9 and a.unit == "m" and a.confidence == NO.CONF
    assert NO.listed_sum_answer("x.png", "What is the total length of the four trenches listed in the notes?", tl) is None
    assert NO.listed_sum_answer("x.png", "How long is the pipe trench?", tl) is None


def test_subscope_and_sqft_patterns():
    from stoa.solvers.raster import apartment as AP
    m = AP._SUBSCOPE_N.search("total floor area of all rooms in the entry vestibule module (module 01)?")
    assert m and m.group(2) == "01"
    assert AP._SUBSCOPE_N.search("What is the total floor area of this floor?") is None
    assert AP._SUBSCOPE_NAMED.search("the four private offices in the administration module").group(0) == "module"
    assert AP._SUBSCOPE_NAMED.search("total area of the second floor") is None
    assert AP._SQFT.search("108 SF") and not AP._SQFT.search("10.5 m²")
