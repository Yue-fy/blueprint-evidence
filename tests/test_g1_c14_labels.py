"""G1 3eabd058's label test (UX 02:34 / 02:35 BLOCKER-candidate), taken alone into the 03:30 freeze by S0: the rest of
tests/test_g1_c14.py on night-G1 belongs to G1 commits that are not in the freeze."""
from stoa.contract import Answer


def test_clarify_labels_never_show_trace_json_or_solver_ids():
    from stoa.agent import vision as V

    g = Answer(value=415.28, confidence=0.3, trace=['recognised: {"kind": "plan_render", "scale": null}',
                                                     "Gemini 3.1 Pro vision: area 415 m2"])
    assert V.label_of(g) == "Gemini 3.1 Pro"
    assert V.label_of(Answer(value=1, trace=['recognised: {"kind": "x vision y"}'])) == "Gemini"
    assert V._our(Answer(value=1, solver="pipeline/plan_rooms")) == "our measured outline reading"
    assert "plan_rooms" not in V._our(Answer(value=1, solver="pipeline/plan_rooms"))
