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
