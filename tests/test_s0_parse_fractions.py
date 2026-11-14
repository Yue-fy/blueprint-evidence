"""S0 (D1 21:20, T1 21:25): the scorer reads fractions, mixed numbers and feet-inch strings."""
import pytest

from stoa.eval.scoring import parse_number, parse_quantity, score


@pytest.mark.parametrize("s, want", [
    ("2 1/2", 2.5), ("2½", 2.5), ("2-1/2", 2.5), ("3 ¾", 3.75), ("1/16", 0.0625), ('3/4"', 0.75),
    ("12", 12.0), ("12,5", 12.5), ("1 234.5 m2", 1234.5), ("about 7", 7.0), ("-3", -3.0), ("1:100", 1.0),
    ("14/11/2026", 14.0), ("12-15", 12.0), ("1747-18743", 1747.0),
])
def test_numbers(s, want):
    assert parse_number(s) == pytest.approx(want)


@pytest.mark.parametrize("s, inches", [
    ("2'-10\"", 34), ("9'-0\"", 108), ("2' 10\"", 34), ("13'-1 1/2\"", 157.5), ("5'-1/2\"", 60.5), ("2'-10", 34),
    ("9'", 108), ("2’-10”", 34),
])
def test_feet_inches(s, inches):
    assert parse_quantity(s) == (pytest.approx(inches), "in")


def test_score_feet_inches_and_fractions():
    assert score("length", 0.864, "m", "2'-10\"", None) is True
    assert score("length", 2.743, "m", "9'-0\"", None) is True
    assert score("length", 2.8333, "ft", "2'-10\"", "ft") is True   # the string's marks win over the unit field
    assert score("length", "2'-10\"", "ft", 2.8333, "ft") is True   # feet-inch gold
    assert score("length", 2.5, "in", "2 1/2", "in") is True
    assert score("length", 2.5, "in", "2½", None) is True
    assert score("length", 0.75, "in", '3/4"', None) is True
    assert score("length", 0.864, "m", "2'-11\"", None) is False
    assert score("count", 7, None, "7", None) is True
    assert score("area", 12.5, "m2", "12,5 m²", "m2") is True
