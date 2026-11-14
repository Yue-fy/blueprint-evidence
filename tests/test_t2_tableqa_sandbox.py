"""T2 (audit 19:51 risk #3): model-written table-QA code runs on the given DataFrames only.

The audit read a canary file through `_run("answer = pd.read_csv('<path>').iloc[0, 0]", {})`. These tests
repeat that read and a set of escape tricks (each must fail without touching the canary or writing a file),
then check that the kinds of code the local model really writes (harvested from eval traces) still answer.
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from stoa import sandbox
from stoa.solvers.vector import tableqa


@pytest.fixture()
def canary(tmp_path):
    p = tmp_path / "canary.csv"
    p.write_text("audit_canary\n83\n")
    return p


@pytest.fixture()
def tables():
    doors = pd.DataFrame({"row_id": [0, 1, 2, 3], "Type Mark": ["U22", "U22", "B1", "B2"],
                          "Count": ["3", "2", "4", "1"], "Width": ["3'-0\"", "3'-0\"", "2'-8\"", "6'-0\""]})
    rooms = pd.DataFrame({"room_id": [10, 11, 12], "name": ["Kitchen", "Office 101", "Office 102"],
                          "area_m2": [12.5, 20.0, 18.25]})
    return {"t1_door_schedule": doors, "rooms": rooms}


def test_audit_canary_read_is_refused(canary):
    with pytest.raises(ValueError):
        tableqa._run(f"answer = pd.read_csv({str(canary)!r}).iloc[0, 0]", {})


ESCAPES = [
    # module internals that lead to os / builtins
    "answer = pd.io.common.get_handle({p!r}, 'r').handle.read()",
    "answer = statistics.sys.modules['os'].listdir('/')",
    "answer = re.enum.sys.modules['builtins'].open({p!r}).read()",
    # class / frame introspection back to real globals
    "answer = [c for c in ().__class__.__base__.__subclasses__()][0]",
    "g = (x for x in [1])\nanswer = g.gi_frame.f_back.f_globals['pd'].read_csv({p!r})",
    "answer = tableqa_env['__builtins__']['open']({p!r}).read()",
    # pandas string dispatch to an I/O method, literal or built at run time
    "answer = t1_door_schedule.agg('to_csv', {p!r})",
    "m = 'to_' + 'csv'\nanswer = t1_door_schedule.agg(m, {p!r} + '.out')",
    "answer = t1_door_schedule.to_pickle({p!r} + '.pkl')",
    "answer = t1_door_schedule.values.tofile({p!r} + '.bin')",
    # pandas' own expression language reaching the namespace
    "answer = t1_door_schedule.query(\"@pd.read_csv('x')\")",
    "q = 'Count > 1'\nanswer = t1_door_schedule.query(q)",
    # reflection through format strings, imports, try/except swallowing the deadline
    "answer = '{{0.gi_frame}}'.format(x for x in [1])",
    "import os\nanswer = os.listdir('/')",
    "try:\n    answer = 1\nexcept:\n    pass",
]


@pytest.mark.parametrize("code", ESCAPES)
def test_escape_tricks_fail(code, canary, tables, tmp_path):
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises((ValueError, PermissionError, AttributeError, NameError, TypeError, KeyError)):
        res = tableqa._run(code.format(p=str(canary)), tables)
        assert res["value"] != 83  # pragma: no cover - reached only if nothing raised
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # nothing written


def test_runtime_built_io_name_is_stopped_by_the_audit_hook(canary, tables):
    """The AST check cannot see a method name assembled at run time; the audit hook refuses the write."""
    code = "u = '-_'[1]\nm = 'to' + u + 'csv'\nanswer = t1_door_schedule.agg(m, 0, " + repr(str(canary) + ".out") + ")"
    with pytest.raises(PermissionError):
        sandbox.run(code, tables, tableqa.SAFE_BUILTINS)
    assert not (canary.parent / "canary.csv.out").exists()


def test_infinite_loop_hits_the_deadline(tables):
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        sandbox.run("n = 0\nwhile True:\n    n = n + 1\nanswer = n", tables, tableqa.SAFE_BUILTINS, time_limit=0.5)
    assert time.monotonic() - t0 < 5


def test_hook_is_off_outside_the_sandbox(canary):
    tableqa._run("answer = 1 + len([1, 2])\nused = []", {})
    assert canary.read_text().startswith("audit_canary")  # this test process can still read files


# what the local model actually writes (harvested from eval traces, 'table QA code: ...')
REAL = [
    ("answer = t1_door_schedule[t1_door_schedule['Type Mark'] == 'U22']['Count'].astype(int).sum()\n"
     "unit = \"count\"\nused = t1_door_schedule[t1_door_schedule['Type Mark'] == 'U22']['row_id'].tolist()", 5),
    ("answer = pd.to_numeric(t1_door_schedule['Count']).sum()\nunit = 'count'\n"
     "used = t1_door_schedule['row_id'].tolist()", 10),
    ("office_rooms = rooms[rooms['name'].str.contains('Office', case=False, na=False)]\n"
     "answer = round(office_rooms['area_m2'].sum(), 2)\nunit = 'm2'\nused = office_rooms['room_id'].tolist()", 38.25),
    ("r = rooms.query(\"area_m2 > 15\")\nanswer = len(r)\nunit = 'count'\nused = r['room_id'].tolist()", 2),
    ("answer = rooms.loc[rooms['area_m2'].idxmax(), 'name']\nunit = None\n"
     "used = [rooms.loc[rooms['area_m2'].idxmax(), 'room_id']]", "Office 101"),
    ("w = t1_door_schedule['Width'].str.extract(r\"(\\d+)'-(\\d+)\")\n"
     "answer = int((w[0].astype(int) * 12 + w[1].astype(int)).max())\nunit = None\nused = []", 72),
    ("rows = [r for r in t1_door_schedule.itertuples() if r.Count != '1']\nanswer = len(rows)\n"
     "unit = 'count'\nused = [r.row_id for r in rows]", 3),
    ("answer = f\"{rooms['area_m2'].mean():.1f}\"\nunit = None\nused = []", "16.9"),
    ("answer = statistics.median(rooms['area_m2'].tolist()) + math.floor(0.5)\nunit = 'm2'\nused = []", 18.25),
    ("answer = bool(pd.isna(rooms['name']).any()) or re.search(r'\\d+', 'Office 7') is not None\nused = []", True),
]


@pytest.mark.parametrize("code,want", REAL)
def test_real_table_code_still_answers(code, want, tables):
    assert tableqa._run(code, tables)["value"] == want


def test_tables_are_copies(tables):
    tableqa._run("rooms['area_m2'] = 0\nanswer = len(rooms)\nused = []", tables)
    assert tables["rooms"]["area_m2"].sum() == pytest.approx(50.75)
