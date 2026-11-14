"""T1 count evidence completeness (independent audit 19:51, risk #1): a count's marks must cover the count, or the
answer says it is partial and never gains confidence from an agreement it cannot show.

Audit reproduction (no network): Gemini returns 20 door boxes -> read() kept 12 and blamed Gemini ('said 20 but drew
12 boxes'); agreement with an evidence-less base 20 @0.4 -> 20 / 12 boxes / 0.8 / missing=[]. The model is stubbed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from stoa.contract import Answer, Evidence

Q = "How many doors are on this plan?"


@pytest.fixture
def gemini_stub(tmp_path, monkeypatch):
    from PIL import Image

    import stoa.llm as llm

    img = tmp_path / "plan.png"
    Image.new("RGB", (1200, 900), "white").save(img)

    def set_reply(value: int, boxes: int):
        ev = [{"label": f"door {i + 1}", "box_2d": [10 + 40 * (i // 20), 10 + 40 * (i % 20),
                                                    40 + 40 * (i // 20), 40 + 40 * (i % 20)]} for i in range(boxes)]
        reply = {"answer": str(value), "unit": "count", "how": "counted door swings", "evidence": ev}
        monkeypatch.setattr(llm, "chat", lambda *a, **k: SimpleNamespace(text=json.dumps(reply), prompt_tokens=10,
                                                                          completion_tokens=10))
    return str(img), set_reply


def test_read_keeps_every_box(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 20)
    g = V.read(path, Q)
    assert g.value == 20 and len(g.evidence) == 20
    assert not any("but drew" in a for a in g.assumptions)
    assert g.confidence == V.VISION_CAP


def test_read_says_when_marks_are_partial(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 12)
    g = V.read(path, Q)
    assert len(g.evidence) == 12 and any("said 20 but drew 12" in a for a in g.assumptions)
    assert any("showing 12 of 20" in m for m in g.missing)


def test_agreement_with_partial_marks_is_not_promoted(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 12)
    base = Answer(value=20, unit="count", confidence=0.4, solver="pipeline/vlm_direct", trace=["t"])
    out = V.vision_step(path, Q, base, why="no confident structured count")
    assert out.value == 20 and len(out.evidence) == 12
    assert out.confidence <= 0.4 + 1e-9
    assert any("but drew 12" in a for a in out.assumptions)
    assert any("showing 12 of 20" in m for m in out.missing)


def test_agreement_with_full_marks_is_promoted(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 20)
    base = Answer(value=20, unit="count", confidence=0.4, solver="pipeline/vlm_direct", trace=["t"])
    out = V.vision_step(path, Q, base, why="no confident structured count")
    assert len(out.evidence) == 20 and out.confidence == V.AGREE_CONF


def test_agreement_backed_by_a_marked_printed_total(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 12)
    ev = [Evidence(file=path, page=0, bbox=[1, 1, 50, 10], bbox_space="image_px", note="text 'TOTAL 20 DOORS'")]
    base = Answer(value=20, unit="count", confidence=0.6, evidence=ev, solver="pipeline/raster", trace=["t"])
    out = V.vision_step(path, Q, base, why="no confident structured count")
    assert out.confidence == V.AGREE_CONF  # the printed total is the support, not the 12 boxes


def test_two_samples_with_partial_marks_stay_capped(gemini_stub):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    set_reply(20, 12)
    out = V.vision_step(path, Q, Answer(value=None, trace=["t"]), why="our readers found nothing usable")
    assert out.value == 20 and out.confidence < V.SAMPLES_CONF
    assert any("showing 12 of 20" in m for m in out.missing)


def test_more_boxes_than_the_cap_say_so(gemini_stub, monkeypatch):
    from stoa.agent import vision as V

    path, set_reply = gemini_stub
    monkeypatch.setattr(V, "MAX_BOXES", 15)
    set_reply(20, 20)
    g = V.read(path, Q)
    assert len(g.evidence) == 15 and any("first 15 of 20" in a for a in g.assumptions)
    assert any("showing 15 of 20" in m for m in g.missing)


def test_one_sample_backing_a_partially_marked_count_is_not_promoted(monkeypatch):
    """G1's arbitration (label_count 6, Gemini 5 then 6) with ONE box for six doors: kept, not raised to 0.65."""
    import stoa.agent.vision as V

    ours = Answer(value=6, unit="count", confidence=0.6, solver="pipeline/label_count",
                  evidence=[Evidence(file="x.png", bbox=[1, 1, 5, 5], bbox_space="image_px", note="tag")])
    g = [Answer(value=v, unit="count", confidence=0.45, trace=["Gemini 3.1 Pro vision reading: x"],
                solver="pipeline/gemini_vision") for v in (5, 6)]
    it = iter(g)
    monkeypatch.setattr(V, "read", lambda *a, **k: next(it, None))
    out = V.vision_step("/tmp/upload/x.png", "How many doors does the filter building have?", ours, why="test")
    assert out.value == 6 and out.confidence <= 0.6


def test_door_detect_marks_every_counted_symbol(tmp_path, monkeypatch):
    """X1 sl-086 (DfE PNG): door_detect answered 84 with 60 marks (evidence was boxes[:60]). Detector stubbed."""
    from PIL import Image

    import stoa.agent.vision as V
    import stoa.solvers.label_count as LC
    from stoa.solvers import door_detect as D

    img = tmp_path / "plan.png"
    Image.new("RGB", (4000, 3000), "white").save(img)
    monkeypatch.setattr(LC, "_render_source", lambda p: None)
    monkeypatch.setattr(LC, "_image_lines", lambda p: [("PLAN", [0, 0, 1, 1])])
    monkeypatch.setattr(D, "_up", lambda: True)
    monkeypatch.setattr(V, "_image_bar", lambda p: SimpleNamespace(px_per_m=46.0))
    boxes = [[10 + 45 * (i % 80), 10 + 45 * (i // 80), 55 + 45 * (i % 80), 55 + 45 * (i // 80), 0.9, "door"]
             for i in range(84)]
    monkeypatch.setattr(D, "_detect", lambda img, zoom, timeout: {"boxes": boxes})
    a = D.DoorDetect().solve(str(img), "How many doors are on this plan?")
    assert a.value == 84 and len(a.evidence) == 84
    monkeypatch.setattr(D, "MAX_BOXES", 50)
    b = D.DoorDetect().solve(str(img), "How many doors are on this plan?")
    assert b.value == 84 and len(b.evidence) == 50 and any("showing 50 of 84" in m for m in b.missing)
