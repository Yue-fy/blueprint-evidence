"""Stoa kind 2 (captain 16:15): skirting / baseboard / perimeter questions are LENGTHS, never counts (X1 16:38, E1 16:46)."""
import pytest

from stoa.router.intent import infer_task


@pytest.mark.parametrize("q,unit", [
    ("How many linear metres of skirting board does the laundry need (room perimeter minus the door opening)?", "m"),
    ("How many running meters of skirting for the hall?", "m"),
    ("How many lineal feet of baseboard in the dormitory?", "ft"),
    ("How many LF of base are needed for room 101?", "ft"),
    ("How much baseboard is needed in the Multi-Purpose Hall?", "m"),
    ("How many skirting boards does the kitchen need?", "m"),
    ("Wie viele Meter Sockelleiste braucht die Küche?", "m"),
    ("Kuinka paljon jalkalistaa tarvitaan olohuoneeseen?", "m"),
    ("What is the perimeter of the kitchen?", "m"),
])
def test_skirting_is_length(q, unit):
    assert infer_task(q) == ("length", unit)


@pytest.mark.parametrize("q,task", [
    ("How many square metres of floor does the laundry have?", "area"),
    ("How many doors are on the plan?", "count"),
    ("How many perimeter walls are there?", "count"),
])
def test_other_tasks_unchanged(q, task):
    assert infer_task(q)[0] == task


def test_pipe_size_is_text():
    """G1 c7 (QA 18:56): 'What size is the suction header?' was an AREA question; rooms keep 'size' = area."""
    from stoa.router.intent import parse

    for q in ("What size is the suction header?", "What size is the pipe going to the truck fill stand?",
              "What is the size of the 12\" fire water line?", "What pipe size feeds the truck fill stand?",
              "What is the line size of the suction header?"):
        assert parse(q).task == "text", q
    for q in ("What is the size of the kitchen?", "What size is the main hall?", "How big is the genset foundation?"):
        assert parse(q).task == "area", q
