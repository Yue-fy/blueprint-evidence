"""T1 c9 (V1 02:16): the dataset's own drawing families read by their own reader keep that reader's confidence."""
from __future__ import annotations

from stoa.contract import Answer, Evidence
from stoa.trust import calibrate as C
from stoa.trust import support as S

REC = 'recognised: {"kind": "%s", "label": "x", "confidence": 0.97, "scale": "1:100", "pages": 1}'


def _ans(kind, solver, value=3, conf=0.94, ev=None, unit="count"):
    ev = ev if ev is not None else [Evidence(file="d.pdf", page=0, bbox=[10 + 40 * i, 10, 40 + 40 * i, 40],
                                             note=f"bedroom {i + 1}") for i in range(int(value))]
    a = Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=solver,
               trace=[REC % kind, "intent: count bedroom [drawing] (lang en)"])
    return S.mark_support("How many bedrooms are drawn on this floor plan?", a)


def test_swiss_plan_read_by_pdf_vector_keeps_its_confidence():
    a = C.calibrate(_ans("swiss_plan", "pipeline/pdf_vector"), "d.pdf")
    assert a.confidence == 0.94
    assert any("reader's own confidence is kept as the floor" in t and "swiss_plan" in t for t in a.trace)


def test_zero_count_on_family_reader_is_not_shown_as_a_guess():
    a = C.calibrate(_ans("swiss_plan", "pipeline/pdf_vector", value=0, conf=0.95, ev=[]), "d.pdf")
    assert a.confidence == 0.95


def test_other_reader_or_other_kind_is_still_calibrated():
    for kind, solver in (("swiss_plan", "pipeline/vlm_lora"), ("cad_sheet", "pipeline/pdf_vector"),
                         ("bim_plan", "pipeline/pdf_text")):
        a = C.calibrate(_ans(kind, solver), "d.pdf")
        assert any(t.startswith(C.TRACE_PREFIX) for t in a.trace), (kind, solver)
        assert not any("reader's own confidence is kept" in t for t in a.trace), (kind, solver)


def test_the_map_may_still_raise_a_family_reader():
    a = C.calibrate(_ans("bim_plan", "pipeline/plan_rooms", conf=0.30), "d.pdf")
    assert a.confidence >= 0.30


def test_family_kill_switch(monkeypatch):
    monkeypatch.setenv("STOA_T1_FAMILY", "0")
    a = C.calibrate(_ans("bim_plan", "pipeline/plan_rooms"), "d.pdf")
    assert not any("reader's own confidence is kept" in t for t in a.trace)


def test_recognised_kind_reads_the_trace():
    assert C.recognised_kind(_ans("bim_plan", "pipeline/plan_rooms")) == "bim_plan"
    assert C.recognised_kind(Answer(value=1, trace=["x"])) == ""


def test_uncounted_count_with_a_high_own_confidence_keeps_it_below_very_sure():
    """24c7db70 (V1 01:47): a count nothing located but whose reader's own calibrated confidence is >= 0.9 (kept by
    sanity.uncounted_guard) shows that confidence capped at 0.89, not the unlocated cap 0.39; lower ones stay a guess."""
    from stoa.trust import sanity as SA

    def unl(n, conf, q, kind="cubicasa_plan"):
        a = Answer(value=n, unit="count", confidence=conf, evidence=[], solver="pipeline/vlm_lora",
                   trace=[REC % kind, "intent: count room [drawing] (lang en)"])
        return S.mark_support(q, SA.uncounted_guard(q, a))

    a = unl(1, 0.97, "How many bathrooms are there?")
    assert (a.support or {}).get("state") == "unlocated"
    a = C.calibrate(a, "/tmp/x/plan.png")
    assert 0.85 <= a.confidence <= C.NOT_SUPPORTED_MAX, a.trace
    low = C.calibrate(unl(3, 0.7, "How many doors are there?"), "/tmp/x/sheet.png")
    assert low.confidence <= C.UNLOCATED_MAX
    # outside the CubiCasa-style pictures it was measured on, an unlocated count stays a guess
    other = C.calibrate(unl(4, 0.95, "How many doors are there?", kind="cad_sheet"), "/tmp/x/sheet.png")
    assert other.confidence <= C.UNLOCATED_MAX, other.trace
