"""T1 sanity guards (captain 20:47 / 20:49; expert Ziying's workbench rules, tab 3).

Rule 1, verbatim: 'A facade of habitable rooms (classrooms, offices, bedrooms) has at least one window; a count of 0
must be flagged.' Live failure (night a486439 and this head before the guard): Stoa's DfE sheet 1, 'How many windows
are on the west facade of the classroom wing?' -> 0 @0.30 (raster_cc5k on a 150-dpi render), expert gold 18.
Rule 2, verbatim: 'Show the 90% range only when the drawing is in the calibration domain (CubiCasa5K flats).'"""

from __future__ import annotations

import re

import pytest

from stoa.contract import Answer, Evidence
from stoa.trust import sanity as S

DFE_Q = "How many windows are on the west facade of the classroom wing?"
CARD_RANGE = re.compile(r"interval(\d{2}):\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")  # web/src/card.ts


def _dfe_answer(**kw) -> Answer:
    """The live answer as it came out of the image pass (experiments/t1/ask.py on the DfE PDF, 21:00)."""
    a = Answer(value=0, unit="count", confidence=0.303, solver="pipeline/raster_cc5k",
               evidence=[Evidence(file="sheet1.r150.png", page=0, bbox=[10, 10, 900, 600], bbox_space="image_px",
                                  note="searched area: no window found here (answer 0; approximate extent of the "
                                       "plan)")],
               missing=["a second independent count of window symbols (model exact on ~40% of held-out drawings "
                        "with this count; usually within ±1–2)",
                        "exact count not certain: 90% range 0–3 (held the true count ~90% of the time on held-out "
                        "CubiCasa5K drawings)"],
               trace=["raster_cc5k: asked the canonical form \"How many windows are shown on this floor plan?\"",
                      "raster_cc5k: 0 count (conf 0.30, held-out hit rate)", "[raster_cc5k] interval90: [0, 3]"])
    a.recognition = {"kind": "cad_sheet", "label": "architectural drawing sheet (CAD/Revit)"}
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_ziying_rule_verbatim_facade_of_habitable_rooms_zero_windows_is_flagged():
    out = S.sanity_guard(DFE_Q, _dfe_answer())
    assert out.value is None and out.confidence == 0.0  # never an answer with a confidence
    assert out.missing[0].startswith("none found — probably a reading failure")  # W5's no-number card sentence
    assert "has at least one window" in out.missing[0]
    assert "count them by hand" in out.missing[0] and "Ask about an area" in out.missing[0]  # the manual way
    assert "west facade of the classroom wing" in out.missing[0]
    assert any("searched area" in (e.note or "") for e in out.evidence)  # what was searched stays marked
    assert not any("90% range" in m for m in out.missing)
    assert not any(CARD_RANGE.search(t) for t in out.trace)  # the card draws no range
    assert any("found 0 windows" in a for a in out.assumptions)
    assert S.flagged_zero(out)


@pytest.mark.parametrize("q", [
    "How many windows are shown on this floor plan?",
    "Kuinka monta ikkunaa pohjapiirustuksessa on?",
    "How many windows does the house have in total?",
    "How many doors are shown on this drawing?",
    "How many doors does the pump house have?",          # X1 sealed: gold 2, ours 0
    "How many doors does the filter building have?",     # scan_dev: gold 6, ours 0
    "How many doors open into the general office?",      # gold 1, ours 0
    "How many windows does the bedroom have?",
    "How many windows does the classroom have?",
    "How many windows are on the north elevation?",
])
def test_implausible_zeros_are_flagged(q):
    a = Answer(value=0, unit="count", confidence=0.6, solver="pipeline/door_detect", trace=[])
    out = S.zero_guard(q, a)
    assert out.value is None and S.flagged_zero(out), q


@pytest.mark.parametrize("q", [
    "How many windows does the bathroom have?",           # may have none
    "How many windows are in the corridor?",
    "How many windows does the storage room have?",
    "How many windows does the plant room have?",
    "How many doors does the corridor have?",             # open to the next space
    "How many double doors does the office have?",        # a subtype may be 0
    "How many fire doors are on the plan?",
    "How many doors in the door schedule are type D2?",
    "By how many doors does this plan differ from the IFC model?",   # kg_qa: gold 0
    "How many doors are on the floor below this one?",               # ifc: gold 0
    "How many exterior doors does the flat have?",
    "How many windows and doors are on this plan?",       # a sum: the parts guard's business
    "Hur många bastur finns på planritningen?",           # saunas, not doors
    "How many rooms are on this plan?",
])
def test_zero_where_zero_is_plausible_is_left_alone(q):
    a = Answer(value=0, unit="count", confidence=0.6, solver="pipeline/door_detect", trace=[])
    out = S.zero_guard(q, a)
    assert out.value == 0 and out.confidence == 0.6, q


def test_nonzero_count_is_left_alone():
    a = _dfe_answer(value=18, confidence=0.55)
    a.recognition = {"kind": "cubicasa_plan", "label": "raster floor plan (CubiCasa style)"}
    a.trace = a.trace[1:]  # no whole-plan rewrite (that case: test_whole_plan_count_is_not_the_facade_count)
    out = S.sanity_guard(DFE_Q, a)
    # the zero rule / range rule leave a non-zero count alone; T1 c5 (captain 23:04): its only box is a searched area,
    # so it is flagged 'not counted (no symbols located)' and capped (value kept)
    assert out.value == 18 and out.missing[1:] == a.missing and out.trace[:len(a.trace)] == a.trace
    assert out.missing[0].startswith(S.UNCOUNTED) and out.confidence == S.UNCOUNTED_CAP
    a.evidence = [*a.evidence, Evidence(file="sheet1.r150.png", page=0, bbox=[20, 20, 30, 30], note="window")]
    out = S.sanity_guard(DFE_Q, a)
    assert out.value == 18 and out.confidence == 0.55 and out.missing == a.missing and out.trace == a.trace


def test_structured_model_zero_is_kept_but_flagged():
    """Swiss DXF 'Wie viele Fenster …' gold 0 was answered 0 by dxf: what the model holds stays, flagged."""
    a = Answer(value=0, unit="count", confidence=0.95, solver="pipeline/dxf", trace=[])
    out = S.zero_guard("Wie viele Fenster sind auf diesem Grundriss eingezeichnet?", a)
    assert out.value == 0 and out.confidence == S.FLAG_CONF
    assert out.missing[0].startswith("0 windows in the model's data — unusual")
    a = Answer(value=0, unit="count", confidence=0.9, solver="pipeline/ifc", trace=[])
    assert S.zero_guard("How many doors are in this building?", a).value == 0


def test_text_answers_and_other_kinds_are_untouched():
    for v, u in (("0", "m2"), (0.0, "m"), ("none", "text")):
        a = Answer(value=v, unit=u, confidence=0.5, solver="pipeline/x", trace=[])
        assert S.zero_guard("How many windows are on this plan?", a).value == v


# --- rule 2: the calibrated range only on CubiCasa5K flats ----------------------------------------------------------
def test_range_hidden_off_domain():
    a = _dfe_answer(value=3, confidence=0.3)
    out = S.range_guard(a)
    assert not any("90% range" in m for m in out.missing)
    assert any("calibrated on CubiCasa5K flat plans only" in m for m in out.missing)
    assert not any(CARD_RANGE.search(t) for t in out.trace)
    assert any(t.startswith("[raster_cc5k] range not shown") for t in out.trace)  # the reading stays in the trace
    assert out.value == 3 and out.confidence == 0.3  # only the range goes


def test_range_kept_on_cubicasa_flats():
    a = _dfe_answer(value=3, confidence=0.3)
    a.recognition = {"kind": "cubicasa_plan", "label": "raster floor plan (CubiCasa style)"}
    out = S.range_guard(a)
    assert out.missing == a.missing and out.trace == a.trace


def test_range_uses_the_uploaded_files_kind_over_the_render():
    """A PDF page rendered at 150 dpi is itself recognised as 'cubicasa_plan' (DfE sheet 1): the upload passes the
    PDF's kind."""
    a = _dfe_answer(value=3, confidence=0.3)
    a.recognition = {"kind": "cubicasa_plan", "label": "raster floor plan (CubiCasa style)"}
    out = S.range_guard(a, kind="cad_sheet")
    assert not any(CARD_RANGE.search(t) for t in out.trace)
    assert any("architectural drawing sheet (CAD/Revit)" in m for m in out.missing)


def test_range_kept_when_domain_unknown():
    a = _dfe_answer(value=3, confidence=0.3)
    a.recognition = None
    assert S.range_guard(a).trace == a.trace
    assert S.range_guard(a, path="data/cubicasa5k/colorful-1.png").trace == a.trace


def test_pipeline_and_upload_call_the_guards():
    import inspect

    from stoa import pipeline
    from stoa.solvers import upload

    assert "_sanity.sanity_guard(question, ans, path)" in inspect.getsource(pipeline.Pipeline.solve) or \
        "_sanity.sanity_guard(question, ans, path)" in inspect.getsource(pipeline)
    src = inspect.getsource(upload)
    assert "not _flagged_zero(ans)" in src and "range_guard(ans, kind=" in src


def test_upload_prefers_the_flagged_zero_over_the_prior(monkeypatch, tmp_path):
    """_as_image: the image pass's 'none found' (searched area marked) beats the PDF pass's dataset prior."""
    from stoa.solvers import upload as U
    from stoa.pipeline import Pipeline

    flagged = S.zero_guard(DFE_Q, _dfe_answer())
    monkeypatch.setattr(Pipeline, "solve", lambda self, path, q: flagged.model_copy(deep=True))
    import stoa.formats.normalize as N

    monkeypatch.setattr(N, "render_png", lambda p: (str(tmp_path / "x.r150.png"), 150 / 72))
    monkeypatch.setattr(N, "seed_text_layer", lambda *a, **k: 0)

    class Prep:
        path, paper_f, kind, notes = str(tmp_path / "x.pdf"), 1.0, "page", []

    prior = Answer(value=4, unit="count", confidence=0.106, solver="pipeline/prior",
                   trace=['recognised: {"kind": "cad_sheet"}', "prior[key]: 4 count"])
    up = U.Upload.__new__(U.Upload)
    out = U.Upload._as_image(up, Prep(), DFE_Q, prior)
    assert out.value is None and S.flagged_zero(out)


# --- the expert's rules as H exported them (human/expert_rules.jsonl, owner T1), kept here verbatim -----------------
EXPERT_RULES = {
    "xr-06": "Sanity check: a facade with classrooms behind it always has windows; 0 is not a plausible answer.",
    "xr-20": "A facade of habitable rooms (classrooms, offices, bedrooms) has at least one window; a count of 0 must be "
             "flagged.",
    "xr-21": "Show the 90% range only when the drawing is in the calibration domain (CubiCasa5K flats).",
    # xr-19: W5's web/src/measure.ts ('≈ ±N % (clicking accuracy)'); xr-36: stoa/trust/chaincheck.py (tests/test_t1_c4.py)
    "xr-19": "State the measuring uncertainty from clicking accuracy with the result (≈ ±3 % for a hand-clicked area).",
    "xr-36": "Check that the dimension strings add up to the printed overall dimension before using them (500 + 11000 + 500 = 12000).",
}


@pytest.mark.parametrize("q", [
    "How many windows are on the west facade of the classroom wing?",   # dfe-08 (xr-06)
    "How many windows does the east facade of the office block have?",
    "How many windows are on the south elevation of the bedroom wing?",
    "How many windows are there on the classroom facade?",
])
def test_expert_rule_facade_zero(q):
    out = S.zero_guard(q, Answer(value=0, unit="count", confidence=0.5, solver="pipeline/raster_cc5k", trace=[]))
    assert out.value is None and S.flagged_zero(out), q


def test_expert_rules_file_still_says_this():
    """If H's export changes a T1 rule, this test fails so the guard is re-read against the new wording."""
    import json
    import os
    from pathlib import Path

    f = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "human" / \
        "expert_rules.jsonl"
    if not f.exists():
        pytest.skip("expert_rules.jsonl not on this machine")
    rows = {r["id"]: r for r in map(json.loads, f.read_text().splitlines()) if r.get("owner") == "T1"}
    for k, rule in EXPERT_RULES.items():
        if k in rows:
            assert rows[k]["rule"] == rule, k


# --- the whole plan counted for a facade question --------------------------------------------------------------------
def test_whole_plan_count_is_not_the_facade_count():
    a = _dfe_answer(value=45, confidence=0.303)
    a.recognition = {"kind": "cubicasa_plan"}
    out = S.sanity_guard(DFE_Q, a)
    assert out.value is None and S.flagged(out)
    assert out.missing[0].startswith("this counts the whole plan, not only the west facade of the classroom wing")
    assert "45 windows on the whole plan (an upper bound)" in out.missing[0]
    assert not any(CARD_RANGE.search(t) for t in out.trace)


def test_scope_kept_when_the_reader_got_the_scope_or_no_rewrite():
    a = _dfe_answer(value=18, confidence=0.5)
    a.recognition = {"kind": "cubicasa_plan"}
    a.trace = ["dxf: asked the canonical form \"How many windows are on the west facade?\""]
    assert S.scope_guard(DFE_Q, a).value == 18
    a.trace = ["openings: 18 windows on the west wall"]
    assert S.scope_guard(DFE_Q, a).value == 18
    b = _dfe_answer(value=12, confidence=0.5)  # a whole-plan question with a rewrite: nothing to say
    assert S.scope_guard("How many windows are shown on this floor plan?", b).value == 12


def test_range_kept_on_dataset_cubicasa_files_recognised_as_scans():
    """8.5 % of CubiCasa5K dataset images are recognised as 'scan' / 'plan_render'; they are the calibration family."""
    a = _dfe_answer(value=3, confidence=0.3)
    a.recognition = {"kind": "scan", "label": "scanned / historical drawing"}
    assert S.range_guard(a, path="data/cubicasa5k/high_quality_architectural-5887.png").trace == a.trace
    assert S.range_guard(a, path="/tmp/stoa-uploads/ab12/plan.png").trace != a.trace  # an upload: content decides


# --- text answers of the wrong shape (numeric question, text that is not the quantity) -------------------------------
@pytest.mark.parametrize("q, v, ev", [
    ("Di quanti m² il locale 1.06 è più grande del locale 1.04?", "room 1.06 (Zimmer)", []),           # dxf @0.99
    ("What is the combined net floor area of all rooms (spaces) on '2. Level'?",
     "The room '2. Level' does not exist on the 'Attic' storey.", []),                                  # ifc @0.98
    ("How big is the meeting room?", "2'-10\" wide x 6'-8\" MIN high", ["t3_door_schedule row: 102A"]),  # pdf_text @0.85
    ("How deep is the meeting room?", "6'-8\" MIN", ["t3_door_schedule row: 102A"]),
    ("How long is the meeting room?", "2'-10\"", ["t3_door_schedule row: 102A"]),
])
def test_text_of_the_wrong_shape_is_capped(q, v, ev):
    a = Answer(value=v, unit=None, confidence=0.9, solver="pipeline/x", trace=[],
               evidence=[Evidence(file="s.pdf", page=0, bbox=[1, 1, 5, 5], note=n) for n in ev])
    out = S.text_shape_guard(q, a)
    assert out.confidence == S.SHAPE_CAP and out.missing[-1].startswith(S.SHAPE_MISSING), q
    from stoa.trust.support import support_state

    assert support_state(q, out)["state"] == "unlocated"


@pytest.mark.parametrize("q, v, ev", [
    ("What size is the suction header?", "16 inches", ["16\" SUCTION HEADER"]),
    ("How long is the building east to west, overall?", "323'-4''", []),
    ("What is the total common space area?", "4,479 SF", []),
    ("Hány ajtó látható a(z) 'KG' szint alaprajzán?", "20 ajtó látható a KG szint alaprajzán.", []),
    ("How wide are the control room doors (102S)?", "2'-10\"", ["t3_door_schedule row: 102S"]),  # asks about a door
    ("Which sheet number is this?", "Sheet 2 of 3 Sheets", []),
    ("What is the gross footprint of the second floor?", "42' - 0\" × 16' - 8\" (12.80 m × 5.08 m)", []),
])
def test_text_answers_that_carry_the_quantity_are_left_alone(q, v, ev):
    a = Answer(value=v, unit=None, confidence=0.9, solver="pipeline/x", trace=[],
               evidence=[Evidence(file="s.pdf", page=0, bbox=[1, 1, 5, 5], note=n) for n in ev])
    assert S.text_shape_guard(q, a).confidence == 0.9, q
