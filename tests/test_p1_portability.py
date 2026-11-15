"""P1: honest cost (stoa/cost.py), API-only mode routing (stoa/llm.py api_route), batch CLI (stoa.cli answer)."""

from __future__ import annotations

import json

import pytest

from stoa import cost, llm


def test_gpu_seconds_priced_at_the_hourly_rate():
    b = cost.breakdown({"gpu_seconds": 3.6, "llm_tokens_in": 0, "llm_tokens_out": 0})
    assert b["gpu_usd"] == pytest.approx(3.6 * cost.GPU_USD_PER_HOUR / 3600)
    assert b["api_usd"] == 0 and b["usd"] == pytest.approx(b["gpu_usd"])


def test_gpu_seconds_billed_from_tokens_not_latency():
    # 25k prompt tokens = 1 s at 25k tok/s, although the call reported 30 s of queueing
    c = {"gpu_seconds": 30.0, "llm_tokens_in": 25_000, "llm_tokens_out": 0}
    assert cost.gpu_seconds_billed(c) == pytest.approx(25_000 / cost.PREFILL_TPS)


def test_api_cost_per_model_and_fallback():
    flash = cost.api_usd("gemini-3-flash-preview", 1_000_000, 1_000_000)
    pro = cost.api_usd("models/gemini-3.1-pro-preview", 1_000_000, 1_000_000)
    assert flash == pytest.approx(sum(cost.API_PRICES["gemini-3-flash-preview"]))
    assert pro > flash
    assert cost.api_price("some-unknown-model") == cost.DEFAULT_API_PRICE
    # recorded per-model price wins over the generic token rate
    c = {"api_tokens_in": 1000, "api_tokens_out": 100, "llm_tokens_in": 1000, "llm_tokens_out": 100,
         "api_calls": 1, "api_usd": 0.123}
    assert cost.breakdown(c)["api_usd"] == pytest.approx(0.123)
    assert cost.breakdown(c)["gpu_usd"] == 0


def test_runner_uses_stoa_cost():
    from stoa.eval import runner

    assert runner.cost_usd is cost.cost_usd
    assert runner.RATES["gpu_usd_per_s"] == pytest.approx(cost.GPU_USD_PER_HOUR / 3600)


def test_api_route_off_by_default(monkeypatch):
    monkeypatch.delenv("STOA_MODE", raising=False)
    assert llm.api_route("local", "cc-v2") == ("local", "cc-v2")
    assert not llm.api_mode()


def test_api_route_reroutes_local_steps(monkeypatch):
    monkeypatch.setenv("STOA_MODE", "api")
    monkeypatch.delenv("STOA_API_MODEL", raising=False)
    monkeypatch.delenv("STOA_API_BACKEND", raising=False)
    assert llm.api_route("local", None) == ("gemini", "gemini-3-flash-preview")
    assert llm.api_route("local", "default") == ("gemini", "gemini-3-flash-preview")
    monkeypatch.setenv("STOA_API_MODEL_TEXT", "gemini-3.1-pro-preview")
    assert llm.api_route("local2", "default") == ("gemini", "gemini-3.1-pro-preview")
    # explicit hosted calls keep their model
    assert llm.api_route("gemini", "gemini-3.1-pro-preview") == ("gemini", "gemini-3.1-pro-preview")
    assert llm.api_route("nvidia", None) == ("nvidia", None)
    assert llm.served_models("local") == frozenset()


def test_api_mode_lora_stand_in_or_fail_fast(monkeypatch):
    monkeypatch.setenv("STOA_MODE", "api")
    monkeypatch.delenv("STOA_API_MODEL_LORA", raising=False)
    monkeypatch.delenv("STOA_API_MODEL", raising=False)
    monkeypatch.delenv("STOA_API_BACKEND", raising=False)
    assert llm.api_route("local", "cc-v2") == ("gemini", "gemini-3-flash-preview")  # default stand-in: the cheap model
    monkeypatch.setenv("STOA_API_MODEL_LORA", "none")
    with pytest.raises(llm.LocalModelUnavailable):
        llm.api_route("local", "cc-v3")
    with pytest.raises(llm.LocalModelUnavailable):  # chat() raises before building a client or calling anything
        llm.chat("How many doors?", backend="local", model="cc-v2")
    monkeypatch.setenv("STOA_API_MODEL_LORA", "gemini-3.1-pro-preview")  # opt-in stand-in for the adapters
    assert llm.api_route("local", "cc-v2") == ("gemini", "gemini-3.1-pro-preview")


def test_cli_answer_batch_and_resume(tmp_path, capsys):
    from stoa import cli

    inp = tmp_path / "questions.jsonl"
    rows = [{"path": "data/cubicasa5k/x.png", "question": "How many doors are shown on this floor plan?",
             "answer": 3, "unit": "count", "id": 1},
            {"question": "a row without a path", "id": 2}]
    inp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = tmp_path / "answers.jsonl"
    assert cli.main(["answer", "--in", str(inp), "--out", str(out), "--solver", "dummy", "--workers", "1",
                     "--quiet"]) == 0
    got = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(got) == 2
    a = next(g for g in got if g["id"] == 1)
    assert a["value"] is not None and a["input_unit"] == "count" and a["answer"] == 3  # input kept, not clobbered
    assert {"confidence", "evidence", "assumptions", "missing", "cost", "solver"} <= set(a)
    assert {"usd", "gpu_usd", "api_usd", "cpu_usd"} <= set(a["cost"])
    b = next(g for g in got if g["id"] == 2)
    assert b["value"] is None and "path" in b["error"]
    # resume: nothing new to answer
    assert cli.main(["answer", "--in", str(inp), "--out", str(out), "--solver", "dummy", "--quiet"]) == 0
    assert len(out.read_text().splitlines()) == 2


def test_cost_tier_badge():
    assert cost.tier({})["tier"] == "cheap"
    assert cost.tier({"llm_calls": 0, "wall_seconds": 2.0})["tier"] == "cheap"
    small = {"llm_calls": 1, "llm_tokens_in": 1500, "llm_tokens_out": 20, "gpu_seconds": 0.08}
    assert cost.tier(small)["tier"] == "in-between"
    flash = {"llm_calls": 1, "llm_tokens_in": 1200, "llm_tokens_out": 5, "api_calls": 1, "api_tokens_in": 1200,
             "api_tokens_out": 5, "api_usd": cost.api_usd("gemini-3-flash-preview", 1200, 5)}
    assert cost.tier(flash)["tier"] == "in-between"
    pro = {**flash, "api_tokens_out": 400, "llm_tokens_out": 400, "api_usd": cost.api_usd("gemini-3.1-pro-preview", 1200, 400)}
    assert cost.tier(pro)["tier"] == "expensive" and "hosted" in cost.tier(pro)["why"]
    scan = {"llm_calls": 19, "llm_tokens_in": 70_000, "llm_tokens_out": 30_000, "gpu_seconds": 30}
    t = cost.tier(scan)
    assert t["tier"] == "expensive" and t["label"] == "Expensive" and "tokens" in t["why"]


def _two_page_set(tmp_path):
    import fitz

    d = fitz.open()
    cover = d.new_page(width=600, height=400)
    cover.insert_text((50, 80), "DRAWING SET - cover sheet", fontsize=20)
    plan = d.new_page(width=600, height=400)
    for i in range(60):  # lots of vector content = a drawing
        plan.draw_line((20 + 9 * i, 20), (20 + 9 * i, 380))
    plan.insert_text((100, 200), "2.07 Kitchen 12.5 m2", fontsize=8)
    plan.insert_text((300, 200), "2.08 Bedroom 14.1 m2", fontsize=8)
    third = d.new_page(width=600, height=400)
    third.insert_text((50, 80), "Door schedule A-601: Basement storage", fontsize=12)
    p = tmp_path / "set.pdf"
    d.save(p)
    return p


def test_pick_page_prefers_the_drawing_and_named_pages(tmp_path):
    import fitz

    from stoa.formats.pages import one_page_copy, pick_page

    p = _two_page_set(tmp_path)
    k, why = pick_page(p, "How many rooms are on this floor plan?")
    assert k == 1 and "drawing" in why
    k2, why2 = pick_page(p, "What does sheet A-601 say about the basement storage?")
    assert k2 == 2 and "names" in why2
    c = one_page_copy(p, k)
    with fitz.open(c) as d:
        assert d.page_count == 1 and "Kitchen" in d[0].get_text()


SVG_SHEET = """<?xml version='1.0' encoding='utf-8'?>
<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="200mm" viewBox="0 0 300 200">
<g class="IfcSpace" data-name="101" data-guid="S1"><path d="M10,10 L110,10 L110,110 L10,110 Z"/></g>
<g class="IfcSpace" data-name="102" data-guid="S2"><path d="M120,10 l50,0 l0,50 l-50,0 z"/></g>
""" + "".join(f"""<g class="cut IfcDoor" data-name="M_Single-Flush:0900 x 2100mm:{i}" data-guid="D{i}">
<path d="M{20 + 10 * i},120 L{24.5 + 10 * i},120 A4.5 4.5 1.5708 0 1 {20 + 10 * i},124.5"/></g>""" for i in range(4)) + "</svg>"


def test_svg_ifc_reads_counts_areas_and_scale(tmp_path):
    from stoa.solvers.svg_ifc import SvgIfc, parse_path, read_svg, scale_of

    pts, radii = parse_path("M1,2 L3,4 A4.5 4.5 1.5708 0 1 5,6 h2 v-1 Z")
    assert pts[:3] == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)] and radii == [4.5, 4.5]  # arc params are not points
    p = tmp_path / "sheet.svg"
    p.write_text(SVG_SHEET)
    mpu, how = scale_of(read_svg(str(p)))
    assert mpu == 0.2 and "1:200" in how  # 0.9 m leaf / 4.5 units = 0.2 m per unit
    s = SvgIfc()
    assert s.solve(str(p), "How many doors are shown on this floor plan?").value == 4
    a = s.solve(str(p), "What is the net floor area of the room 'Office (101)'?")
    assert a.value == 400.0 and a.unit == "m2" and a.evidence and a.evidence[0].bbox_space == "svg"
    assert a.evidence[0].exact and len(a.evidence[0].polygon) == 4  # D29: own coordinates + the room outline
    assert s.solve(str(p), "What is the total net floor area of all rooms on this floor?").value == 500.0
    m = s.solve(str(p), "How many doors are there in the model 'Test building'?")
    assert m.value == 4 and m.confidence <= 0.3 and m.missing
    assert s.solve(str(p), "What is the total net volume of all walls, in m³?").value is None


def test_local_llm_never_seen_up_fails_fast(monkeypatch):
    """RL 16:16: with no model server (fresh clone, no GPU) every local LLM step waited up to 300 s. A server never
    seen up in this process gets LLM_NEVER_UP_WAIT (15 s), and later calls fail at once; one seen up keeps the long
    restart budget."""
    openai = pytest.importorskip("openai")

    from stoa import llm

    monkeypatch.delenv("STOA_MODE", raising=False)  # RL 17:28: API mode reroutes 'local' (release verify sets it)
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    class Down:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    raise openai.APIConnectionError(request=None)

    monkeypatch.setattr(llm, "_client", lambda backend: Down())
    monkeypatch.setattr(llm, "_LOCAL_SEEN_UP", {})
    monkeypatch.setattr(llm, "_LOCAL_DOWN_AT", {})
    monkeypatch.setattr(llm, "served_model", lambda b: "m", raising=False)
    with pytest.raises(RuntimeError):
        llm.chat("hi", backend="local", cache=False)
    assert sum(slept) <= 30  # was up to 300 s
    slept.clear()
    with pytest.raises(RuntimeError):
        llm.chat("hi again", backend="local", cache=False)
    assert sum(slept) <= 2  # held: fail at once


def test_served_model_is_remembered_between_calls(monkeypatch):
    """P1 17:10 profile: chat() asks served_model() for every cache key; a cold 14k-px sheet's text layer is 130 VLM
    calls, and each asked the server's /v1/models again (5.9 s of one question). Remembered for SERVED_MODEL_TTL s."""
    from types import SimpleNamespace

    from stoa import llm

    asked = {"n": 0}

    class Up:
        class models:  # noqa: N801
            @staticmethod
            def list():
                asked["n"] += 1
                return SimpleNamespace(data=[SimpleNamespace(id="default", model_extra={}),
                                             SimpleNamespace(id="qwen-vl", model_extra={})])

    monkeypatch.setattr(llm, "_client", lambda backend: Up())
    monkeypatch.setattr(llm, "_SERVED_MODEL", {})
    monkeypatch.setattr(llm, "api_mode", lambda: False)
    assert [llm.served_model("local") for _ in range(50)] == ["qwen-vl"] * 50
    assert asked["n"] == 1
    llm._SERVED_MODEL["local"] = (llm._SERVED_MODEL["local"][0] - llm.SERVED_MODEL_TTL - 1, "qwen-vl")
    assert llm.served_model("local") == "qwen-vl" and asked["n"] == 2  # re-asked after the TTL


def test_cpu_services_are_priced_and_tiered():
    """Audit 19:51 (cost/latency 4/10): the OCR engine's busy time (cpu_seconds, 8 threads) was recorded but not priced,
    and a row that OCR'd a whole scan on CPU showed 'cheap: no AI model used'. Now cpu_seconds x 8 cores are priced at
    the per-core rate (4.7 busy cores per engine second, measured), and > 10 core-minutes of CPU services lifts the badge to in-between (> 80: expensive)."""
    from stoa import contract, cost

    clean = {"wall_seconds": 3.0}
    assert cost.tier(clean)["tier"] == "cheap"
    ocr_scan = {"wall_seconds": 451.5, "cpu_seconds": 390.0}  # one cold OCR of a scanned sheet (d25 scan_dev p50)
    b = cost.breakdown(ocr_scan)
    assert abs(b["cpu_usd"] - (451.5 + 390.0 * cost.SERVICE_CORES) * cost.CPU_USD_PER_HOUR / 3600) < 1e-9
    t = cost.tier(ocr_scan)
    assert t["tier"] == "in-between" and "core-minutes" in t["why"]
    assert cost.tier({"wall_seconds": 900, "cpu_seconds": 1352})["tier"] == "expensive"  # 6,354 core-s
    assert cost.tier({"wall_seconds": 60, "cpu_seconds": 60})["tier"] == "cheap"  # a table crop / clean sheet
    big_llm = {"wall_seconds": 30, "llm_calls": 30, "llm_tokens_in": 90_000, "gpu_seconds": 12, "cpu_seconds": 100}
    assert cost.tier(big_llm)["tier"] == "expensive"
    assert "core-s" in cost.describe(ocr_scan)
    with contract.CostMeter() as m:
        cost.record_service(10.0, threads=16)  # the door detector: 10 s at 16 threads = 20 s of the 8-thread unit
        cost.record_service(0)  # cache hit: nothing
    assert abs(m.cost.cpu_seconds - 20.0) < 1e-9
    with contract.CostMeter() as m2:
        cost.record_cpu(1594.0)  # O1's measured engine CPU time for one cold 6000 px scan
    assert abs(cost.core_seconds({"cpu_seconds": m2.cost.cpu_seconds}) - 1594.0) < 0.1


def test_hosted_call_has_a_deadline(monkeypatch):
    """QA 20:37: a hosted vision call that timed out and retried held a demo answer 150-190 s while a local reading
    was ready. chat() now bounds a hosted call incl. retries (deadline=, $STOA_HOSTED_DEADLINE_S) and gives each
    attempt at most the time left; local backends keep their restart patience."""
    import time

    from openai import APITimeoutError

    from stoa import llm

    calls = []

    class Fake:
        def with_options(self, timeout=None):
            calls.append(timeout)
            return self

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kw):
            time.sleep(0.3)
            raise APITimeoutError(request=None)

    monkeypatch.setattr(llm, "_client", lambda b: Fake())
    monkeypatch.setattr(llm, "_nvidia_throttle", lambda b: None)
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_LLM_REPLAY", raising=False)
    t0 = time.time()
    with pytest.raises(RuntimeError, match="deadline"):
        llm.chat("hi", backend="nvidia", deadline=3.0, cache=False)
    assert time.time() - t0 < 4.5
    assert calls and all(t <= 3.0 for t in calls)


# --- X1 23:27: a file's one-time reading (text layer) is split from the question's own cost -------------------------
def test_file_reading_marks_prep_and_tier_uses_the_question_part():
    from stoa.contract import CostMeter, record_cost

    with CostMeter() as m:
        record_cost(llm_calls=2, llm_tokens_in=4000, llm_tokens_out=200, gpu_seconds=0.36)  # the question's own calls
        with cost.file_reading():
            record_cost(llm_calls=130, llm_tokens_in=480_000, llm_tokens_out=8800, gpu_seconds=28.0)
            with cost.file_reading():  # nested readings are marked once
                record_cost(cpu_seconds=10.0)
    c = m.cost.model_dump()
    assert c["llm_calls"] == 132 and c["prep_llm_calls"] == 130 and c["prep_cpu_seconds"] == 10.0  # totals unchanged
    q = cost.question_part(c)
    assert q["llm_calls"] == 2 and q["llm_tokens_in"] == 4000 and q["cpu_seconds"] == 0
    t = cost.tier(c)
    assert t["tier"] == "in-between" and t["file_reading"]["tier"] == "expensive"
    assert "once" in t["why"] and "130 model calls" in t["why"]
    b = cost.breakdown(c)
    assert b["usd"] == pytest.approx(b["prep_usd"] + b["question_usd"]) and b["prep_usd"] > 10 * b["question_usd"]
    assert "one-time reading" in cost.describe(c)
    assert cost.tier({k: v for k, v in c.items() if not k.startswith("prep_")})["tier"] == "expensive"  # old rows: as before


def test_file_reading_memo_hit_marks_nothing():
    import time

    from stoa.contract import CostMeter

    with CostMeter() as m:
        with cost.file_reading():
            time.sleep(0.01)  # an in-process memo hit: time passes, nothing is billed
    assert not cost.prep_part(m.cost.model_dump()) and cost.tier(m.cost.model_dump())["tier"] == "cheap"
    with cost.file_reading():  # outside any meter: a no-op
        pass


def test_cli_waits_for_a_whole_trace_unless_told(capsys):
    """P1 00:50: the UI's 90 s trace wait relies on a long-lived worker finishing the trace in the background; a CLI
    process exits after its answers, so `ask` / `answer` default to STOA_TRACE_WAIT=600 (an explicit value wins)."""
    import os

    from stoa import cli

    old = os.environ.pop("STOA_TRACE_WAIT", None)
    try:
        assert cli.main(["intent", "How many doors are there?"]) == 0
        assert os.environ["STOA_TRACE_WAIT"] == "600"
        os.environ["STOA_TRACE_WAIT"] = "5"
        assert cli.main(["intent", "How many doors are there?"]) == 0
        assert os.environ["STOA_TRACE_WAIT"] == "5"
    finally:
        os.environ.pop("STOA_TRACE_WAIT", None)
        if old is not None:
            os.environ["STOA_TRACE_WAIT"] = old
    capsys.readouterr()
