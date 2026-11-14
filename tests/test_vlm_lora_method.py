"""VlmLora states its method and, below 0.8, a LoRA-only 90% range (R2, Stoa point 3). Offline: no model call."""
from stoa.contract import Answer
from stoa.solvers.vlm_direct import VlmLora


def _stated(value, conf, missing=None):
    a = Answer(value=value, unit="count", confidence=conf, missing=list(missing or []))
    VlmLora._state_method(VlmLora.__new__(VlmLora), a, "cc-v2")
    return a


def test_method_is_always_an_assumption():
    a = _stated(14, 0.95)
    assert any("fine-tuned 8B vision-language model (cc-v2)" in x for x in a.assumptions)
    assert a.missing == [] and a.value == 14 and a.confidence == 0.95


def test_unsure_count_gets_a_range_that_contains_the_answer():
    for v in (0, 1, 2, 4, 7, 15):
        a = _stated(v, 0.5)
        rng = [m for m in a.missing if "90% range" in m]
        assert len(rng) == 1 and "training drawings" in rng[0]
        lo, hi = VlmLora.lora_range(v)
        assert lo <= v <= hi and lo >= 0 and f"{lo}–{hi}" in rng[0]


def test_existing_range_is_not_duplicated_and_text_values_are_left_alone():
    a = _stated(5, 0.4, missing=["exact count not certain: 90% range 3–6"])
    assert sum("90% range" in m for m in a.missing) == 1
    b = _stated("several", 0.4)
    assert b.missing == [] and b.assumptions


def test_drop_lora_range_removes_only_the_lora_range():
    # pins the coupling with crosscheck.py / pipeline.py, which call drop_lora_range before adding their own range
    a = _stated(4, 0.5, missing=["no located evidence"])
    assert sum("90% range" in m for m in a.missing) == 1
    assert VlmLora.drop_lora_range(a.missing) == ["no located evidence"]
    assert VlmLora.drop_lora_range(["exact count not certain: 90% range 3–6 (detector)"]) == [
        "exact count not certain: 90% range 3–6 (detector)"]
    assert VlmLora.drop_lora_range(None) == []
