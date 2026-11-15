"""T1 c7 (captain 00:31, AUDIT RECHECK-0026): calibrated confidence + 'supported' counts need distinct real objects."""
from __future__ import annotations

from stoa.contract import Answer, Evidence
from stoa.trust import calibrate as C
from stoa.trust import support as S

PS_Q = "How many different pipe support types (PS-...) are called out on this sheet?"


def _tags(n, odd=None):
    ev = []
    for i in range(1, n + 1):
        x = 200 * i
        ev.append(Evidence(file="m101.png", page=0, bbox=[x, 100, x + 90, 136], note=f"PS-{i}"))
    if odd:
        ev.append(Evidence(file="m101.png", page=0, bbox=[5099, 1584, 5186, 1600], note=odd))
    return ev


def _ans(value, ev, conf=0.8, unit="count", trace=None, solver="pipeline/piping_img"):
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=solver,
                  trace=list(trace or ["intent: count unknown [drawing] (lang en)"]))


def test_sl041_ps0_misread_is_not_supported_and_capped():
    a = S.mark_support(PS_Q, _ans(24, _tags(23, odd="PS-0")))
    st = a.support or {}
    assert st.get("state") == "approximate", st
    assert "PS-0" in st["why"] and "misread" in st["why"]
    assert a.confidence <= S.DOUBT_CONF
    assert "doubt" not in st


def test_clean_tag_family_stays_supported():
    a = S.mark_support(PS_Q, _ans(23, _tags(23)))
    assert (a.support or {}).get("state") == "supported"
    assert a.confidence == 0.8


def test_two_marks_on_one_spot_are_not_two_objects():
    ev = [Evidence(file="p.png", page=0, bbox=[10 + 100 * i, 10, 60 + 100 * i, 60], note=f"window {i}") for i in range(4)]
    ev.append(Evidence(file="p.png", page=0, bbox=[12, 11, 61, 59], note="window 9"))
    a = S.mark_support("How many windows are there?", _ans(5, ev, conf=0.7, solver="pipeline/openings"))
    assert (a.support or {}).get("state") == "approximate"
    assert "same spot" in a.support["why"]


def test_stacked_ifc_marks_explained_by_x2_note_pass():
    ev = [Evidence(file="p.pdf", page=0, bbox=[10, 10, 60, 60], note="door (x2 at this spot: D1 + D2 — drawn …)"),
          Evidence(file="p.pdf", page=0, bbox=[10, 10, 60, 60], note="door (x2 at this spot: D1 + D2 — drawn …)")]
    assert S.distinct_marks("How many doors?", ev) is None


def test_calibration_raises_a_printed_supported_area_and_keeps_unlocated_a_guess():
    printed = _ans(61.69, [Evidence(file="s.pdf", page=0, bbox=[1, 1, 5, 5], note="PLANT ROOM 61.69 m2")], conf=0.5,
                   unit="m2", trace=['recognised: {"kind": "cad_sheet"}', "intent: area room [drawing] (lang en)",
                                     "support: supported — the printed value is marked: 'PLANT ROOM 61.69 m2'"])
    C.calibrate(printed, "s.pdf")
    assert printed.confidence >= 0.7 and printed.trace[-1].startswith(C.TRACE_PREFIX)
    guess = _ans(12, [], conf=0.35, trace=['recognised: {"kind": "plan_render"}', "intent: count door [drawing]",
                                          "support: unlocated — nothing on the drawing is marked for this answer"])
    C.calibrate(guess, "a.png")
    assert guess.confidence < 0.4


def test_calibration_respects_caps_text_and_abstentions_and_runs_once():
    capped = _ans(3, [Evidence(file="a.png", page=0, bbox=[1, 1, 5, 5], note="door 1")], conf=0.5,
                  trace=['recognised: {"kind": "plan_render"}', "upload: confidence capped at 0.5 (a vector sheet read "
                         "as a picture)", "support: supported — 3 marks, one per counted item"])
    C.calibrate(capped, "a.png")
    assert capped.confidence <= 0.5
    n = len(capped.trace)
    C.calibrate(capped, "a.png")
    assert len(capped.trace) == n
    none = Answer(value=None, unit="m2", confidence=0.2, trace=["support: unlocated — x"])
    assert C.calibrate(none, "a.png").confidence == 0.2
    unchecked = _ans(4, [], conf=0.6, trace=["intent: count door"])
    assert C.calibrate(unchecked, "a.png").confidence == 0.6
    ifc = _ans(4, [], conf=0.6, trace=['recognised: {"kind": "ifc_model"}', "support: supported — 4 marks, one per "
                                       "counted item"])
    assert C.calibrate(ifc, "m.ifc").confidence == 0.6


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("STOA_T1_CALIBRATE", "0")
    a = _ans(61.69, [], conf=0.5, unit="m2", trace=["support: supported — the printed value is marked: 'X 61.69 m2'"])
    assert C.calibrate(a, "s.pdf").confidence == 0.5


def test_doubt_basis_capped_by_calibration():
    why = "24 marks, but 'PS-0' does not look like the other 23 PS- tags: it may be a misread — check the marked spots"
    assert C.basis(why) == "doubt"
    assert C.bounded(0.95, "approximate", why) <= C.DOUBT_MAX


def test_bare_printed_number_is_its_own_basis():
    assert C.basis("the printed value is marked: '1448'") == "bare"
    assert C.basis("the printed value is marked: 'PLANT ROOM 61.69 m2'") == "printed"
    assert C.basis("the outline is traced or estimated, not read from the drawing's own data: check its shape") == "traced"
    assert C.basis("the marks show where it was read, not the measured shape or printed value") == "readat"


def test_named_tag_family_without_marks_is_not_supported():
    q = "How many filter separators (FSI and FSR) are there?"
    ev = [Evidence(file="m.png", page=0, bbox=[100 * i, 10, 100 * i + 60, 40], note=f"FSI-{i}") for i in range(1, 6)]
    a = S.mark_support(q, _ans(5, ev, conf=0.8))
    assert (a.support or {}).get("state") == "approximate" and "FSR" in a.support["why"]
    assert a.confidence <= S.DOUBT_CONF
    assert C.basis(a.support["why"]) == "doubt"
    both = ev + [Evidence(file="m.png", page=0, bbox=[900, 10, 960, 40], note="FSR-1")]
    assert S.missing_families(q, both) is None
    assert S.missing_families("How many pressure gauges (PG) are tagged?", ev) is None


def test_uncounted_keeps_a_high_reader_confidence_never_very_sure():
    from stoa.trust import sanity as SA

    hi = _ans(2, [], conf=0.95, solver="pipeline/vlm_lora",
              trace=['recognised: {"kind": "cubicasa_plan", "confidence": 0.99}', "intent: count door [drawing] (lang en)"])
    out = SA.uncounted_guard("How many doors are there?", hi, None)
    assert out.value == 2 and out.confidence == 0.95
    lo = _ans(12, [], conf=0.6, solver="pipeline/vlm_lora", trace=["intent: count door [drawing] (lang en)"])
    assert SA.uncounted_guard("How many doors are there?", lo, None).confidence == SA.UNCOUNTED_CAP
    shown = C.calibrate(S.mark_support("How many doors are there?", out), "plan.png")
    # T1 c8 (V1 01:47): such counts are 98.5 % right on the dataset's apartment plans (CubiCasa-style pictures only, c9):
    # kept, but never 'Very sure'
    assert shown.value == 2 and C.UNLOCATED_MAX < shown.confidence <= C.NOT_SUPPORTED_MAX
