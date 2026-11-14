from stoa.solvers.vlm_direct import parse_number


def test_hyphen_inside_an_id_is_not_a_minus_sign():
    # Swiss sheet label read off the drawing (noon dry run 11:34): was -18743
    assert parse_number("1747-18743", integer=False) == 18743.0
    assert parse_number("rooms 12-15", integer=True) == 15
    assert parse_number("door A-12", integer=True) == 12


def test_real_minus_signs_still_parse():
    assert parse_number("ANSWER: -3", integer=True) == -3
    assert parse_number("x = -2.5", integer=False) == -2.5
    assert parse_number("level -1", integer=True) == -1
