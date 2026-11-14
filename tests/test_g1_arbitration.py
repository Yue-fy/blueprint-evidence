"""Captain 18:56 arbitration (QA arbiter r2-r4), replayed on the cached readings: no Gemini call is made."""
import os

import pytest

from stoa.contract import Answer, Evidence


def _g(v, unit):
    return Answer(value=v, unit=unit, confidence=0.45, trace=["Gemini 3.1 Pro vision reading: x"], solver="pipeline/gemini_vision")


def _run(monkeypatch, ans, question, readings, path="/tmp/upload/x.png"):
    import stoa.agent.vision as V

    it = iter(readings)
    monkeypatch.setattr(V, "read", lambda *a, **k: next(it, None))
    return V.vision_step(path, question, ans, why="test")


def test_located_length_is_not_overridden(monkeypatch):
    """DfE laundry skirting: raster 10.01 m (measured outline) ✓ vs Gemini 14.5 -> ours kept, Gemini as alternative."""
    ours = Answer(value=10.01, unit="m", confidence=0.4, solver="pipeline/raster",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 50, 50], bbox_space="image_px", note="outline")])
    out = _run(monkeypatch, ours, "How many metres of skirting board does the laundry need, door openings excluded?",
               [_g(14.5, "m")])
    assert out.value == 10.01 and any("14.5" in m for m in out.missing)


def test_one_gemini_sample_backs_ours(monkeypatch):
    """A-101F doors: label_count 6 ✓, Gemini 5 then 6 -> 6 kept at >= 0.65."""
    # T1 (audit 19:51): one box per counted tag, as label_count draws them; a partial set is not promoted (test_t1_*)
    ours = Answer(value=6, unit="count", confidence=0.6, solver="pipeline/label_count",
                  evidence=[Evidence(file="x.png", bbox=[1 + 9 * i, 1, 5 + 9 * i, 5], bbox_space="image_px", note="tag")
                            for i in range(6)])
    out = _run(monkeypatch, ours, "How many doors does the filter building have?", [_g(5, "count"), _g(6, "count")])
    assert out.value == 6 and out.confidence >= 0.65


def test_unbacked_count_still_yields(monkeypatch):
    """No evidence of ours (a LoRA guess): Gemini's reading is still returned, as before."""
    ours = Answer(value=10, unit="count", confidence=0.34, solver="pipeline/vlm_lora")
    out = _run(monkeypatch, ours, "How many doors are there?", [_g(9, "count"), _g(9, "count")])
    assert out.value == 9


def test_area_unbacked_gemini_does_not_replace(monkeypatch):
    """Areas: Gemini's estimate with no two marked printed factors (here: none) keeps the cross-check path; ours stays."""
    ours = Answer(value=30.845, unit="m2", confidence=0.3, solver="pipeline/raster",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 50, 50], bbox_space="image_px", note="outline")])
    out = _run(monkeypatch, ours, "What is the area of the 150 mm slab-on-grade with D13 @ 300?", [_g(61.69, "m2")])
    assert out.value == 30.845


def _gb(v, marks):
    g = _g(v, "m2")
    g.evidence = [Evidence(file="x.png", page=0, bbox=list(b), bbox_space="image_px", note=f"Gemini 3.1 Pro read: {t}")
                  for t, b in marks]
    return g


_S100 = [("150 SLAB-ON-GRADE W/ D13 @ 300", (6516, 2743, 7012, 2897)), ("6200", (6953, 2967, 7072, 3023)),
         ("9950", (7529, 3423, 7578, 3549))]  # Gemini's boxes on X1's S-100F PNG (cached reading, sl-082)
_TOKS = [("6200", (6964.3, 2984.9, 7053.3, 3023.0)), ("9950", (7543.7, 3446.2, 7578.1, 3534.8)),
         ("6200", (6963.2, 1905.4, 7051.0, 1940.2)), ("4272", (7413.6, 1784.2, 7448.0, 1874.0)), ("0", (0, 0, 1, 1)),
         ("0", (8000, 5000, 8001, 5001))]  # our text layer of that PNG (subset)


def test_area_backed_by_printed_factors_wins(monkeypatch):
    """QA r2-r4 S-100F: raster 30.845 @0.30 ✗ vs Gemini 6200 x 9950 = 61.69 ✓, both numbers in our text layer inside
    Gemini's boxes -> Gemini's area at BACKED_CONF, the two printed numbers as exact evidence, ours as clarify option."""
    import stoa.agent.vision as V

    monkeypatch.setattr(V, "_sheet_tokens", lambda path, space: _TOKS)
    ours = Answer(value=30.845, unit="m2", confidence=0.3, solver="pipeline/raster",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 50, 50], bbox_space="image_px", note="outline")])
    out = _run(monkeypatch, ours, "What is the area of the 150 mm slab-on-grade with D13 @ 300?", [_gb(61.69, _S100)])
    assert out.value == 61.69 and out.confidence == V.BACKED_CONF
    assert [e.exact for e in out.evidence[:2]] == [True, True]
    assert any("30.845" in m for m in out.missing) and any(t.startswith("clarify:") for t in out.trace)


def test_area_factor_not_printed_there_is_not_backed(monkeypatch):
    """Same reading, but the text layer has 9950 nowhere near Gemini's box -> not backed, ours kept."""
    import stoa.agent.vision as V

    monkeypatch.setattr(V, "_sheet_tokens", lambda path, space: [t for t in _TOKS if t[0] != "9950"] +
                        [("9950", (100.0, 100.0, 140.0, 120.0))])
    ours = Answer(value=30.845, unit="m2", confidence=0.3, solver="pipeline/raster",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 50, 50], bbox_space="image_px", note="outline")])
    out = _run(monkeypatch, ours, "What is the area of the 150 mm slab-on-grade with D13 @ 300?", [_gb(61.69, _S100)])
    assert out.value == 30.845


def test_area_wall_subtracted_estimate_is_not_backed(monkeypatch):
    """S-100F D16 slab: Gemini 200.88 = (19800-1200) x (12000-1200): no PAIR of its marked numbers gives it."""
    import stoa.agent.vision as V

    monkeypatch.setattr(V, "_sheet_tokens", lambda path, space: [("19800", (3767, 354, 3871, 389)),
                                                                 ("12000", (875, 2986, 911, 3087))])
    g = _gb(200.88, [("19800", (3754, 344, 3874, 400)), ("12000", (864, 2967, 924, 3100)), ("600", (1510, 442, 1589, 491))])
    assert V.printed_backing("x.png", g) is None


def test_confident_structured_area_is_kept_with_clarify(monkeypatch):
    """A located plan_rooms reading @0.6 vs a backed Gemini product: ours stays (capped 0.5), both offered."""
    import stoa.agent.vision as V

    monkeypatch.setattr(V, "_sheet_tokens", lambda path, space: _TOKS)
    ours = Answer(value=40.0, unit="m2", confidence=0.6, solver="pipeline/plan_rooms",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 50, 50], bbox_space="image_px", note="outline")])
    out = _run(monkeypatch, ours, "What is the area of the 150 mm slab-on-grade with D13 @ 300?", [_gb(61.69, _S100)])
    assert out.value == 40.0 and out.confidence <= 0.5 and any(t.startswith("clarify:") for t in out.trace)


def test_deterministic_openings_count_is_not_escalated():
    """S0/RL d25 replay (sl-088 A-101F doors): DR's openings 6 @0.70 (vector swings, located) went to Gemini
    ('no confident structured count') and was replaced by its single reading 5 @0.30."""
    import stoa.agent.vision as V

    ours = Answer(value=6, unit="count", confidence=0.7, solver="pipeline/openings",
                  evidence=[Evidence(file="x.pdf", bbox=[1, 1, 5, 5], bbox_space="pdf_pt", note="door swing")])
    assert V.wanted(ours, "How many doors does the filter building have?", "/tmp/upload/x.pdf") is None


def test_located_openings_reading_is_kept(monkeypatch):
    ours = Answer(value=6, unit="count", confidence=0.55, solver="pipeline/openings",
                  evidence=[Evidence(file="x.pdf", bbox=[1, 1, 5, 5], bbox_space="pdf_pt", note="door swing")])
    out = _run(monkeypatch, ours, "How many doors does the filter building have?", [_g(5, "count"), None])
    assert out.value == 6
