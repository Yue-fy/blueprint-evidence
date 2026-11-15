"""T2 cycle 7: nothing goes out after the answer was returned.

A hosted step can run on in a background thread after its answer was returned: G1's vision reading waits
STOA_VISION_WAIT (25 s) and then 'the local answer stands', while the request itself may still be queued in stoa.llm's
machine-wide Gemini throttle and be SENT afterwards (the demo's log: a send 1.9 s after the wait ended). The answer's
card was already written, so that drawing went to Google without showing anywhere. netguard now refuses any new
outside lookup / connect from a thread that carries an answer's watch() context once the block has ended.

The 'outside' server is a real HTTP server on 127.0.0.2 (netguard._local_ip patched), reached with the HTTP client the
OpenAI SDK uses, so the audit events and the error path are the real ones. No real hosted call is made."""

from __future__ import annotations

import contextvars
import http.server
import importlib.util
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from stoa import netguard
from stoa.contract import Answer

httpx = pytest.importorskip("httpx2")
ROOT = Path(__file__).resolve().parents[1]
ASK = Path(os.environ.get("T2_ASK_UPLOAD_PATH") or ROOT / "web" / "server" / "ask_upload.py")
OUT = "127.0.0.2"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: list[str] = []
    delay = 0.0

    def _reply(self, body: bytes, ctype: str = "text/plain") -> None:
        time.sleep(_Handler.delay)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        _Handler.hits.append("GET " + self.path)
        self._reply(b"ok")

    def do_POST(self):  # noqa: N802 — an OpenAI-compatible chat completion (what stoa.llm sends to Gemini)
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        _Handler.hits.append("POST " + self.path)
        body = {"id": "x", "object": "chat.completion", "created": 0, "model": "gemini-stub",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps({"answer": "7", "unit": "count"})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        self._reply(json.dumps(body).encode(), "application/json")

    def log_message(self, *a):
        pass


@pytest.fixture()
def outside(monkeypatch):
    try:
        srv = http.server.ThreadingHTTPServer((OUT, 0), _Handler)
    except OSError as e:  # pragma: no cover
        pytest.skip(f"cannot bind {OUT}: {e}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = netguard._local_ip
    monkeypatch.setattr(netguard, "_local_ip", lambda h: False if h.split("%")[0] == OUT else real(h))
    for k in ("STOA_PRIVATE", "STOA_EU_ONLY", "STOA_NETGUARD_LATE"):
        monkeypatch.delenv(k, raising=False)
    assert netguard.install()
    netguard.blocked(clear=True)
    netguard.late(clear=True)
    _Handler.hits.clear()
    _Handler.delay = 0.0
    try:
        yield f"http://{OUT}:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        netguard._forget_all()
        netguard.late(clear=True)


def _late_thread(url: str, go: threading.Event, box: dict, copy: bool = True) -> threading.Thread:
    """A thread an answer starts (context copied, like every thread site in stoa/), which sends only after `go`."""
    def run():
        go.wait(5)
        box["returned_flag"] = netguard.answer_returned()
        try:
            with httpx.Client(timeout=5) as c:
                box["status"] = c.get(url + "/late").status_code
        except httpx.HTTPError as e:
            box["error"] = e

    ctx = contextvars.copy_context() if copy else None
    th = threading.Thread(target=(lambda: ctx.run(run)) if copy else run, daemon=True)
    th.start()
    return th


def _caused_by_netguard(e: BaseException | None) -> bool:
    seen = 0
    while e is not None and seen < 12:
        if isinstance(e, netguard.PrivateNetworkBlocked):
            return True
        e, seen = e.__cause__ or e.__context__, seen + 1
    return False


def test_a_send_after_the_answer_was_returned_is_refused(outside):
    go, box = threading.Event(), {}
    with netguard.watch() as hosts:
        th = _late_thread(outside, go, box)
    go.set()  # the answer is out: now the background step tries to send
    th.join(5)
    assert "status" not in box and _caused_by_netguard(box.get("error")), box
    assert _Handler.hits == []  # nothing reached the outside server
    assert box["returned_flag"] is True
    assert any(OUT in x for x in netguard.late()), netguard.late()
    assert hosts == []  # the answer reached nothing, and says so truthfully
    assert netguard.blocked() == []  # not a private-mode refusal (W5's 'private mode: N refused' line stays empty)


def test_a_send_while_the_answer_runs_is_allowed_and_listed(outside):
    go, box = threading.Event(), {}
    with netguard.watch() as hosts:
        th = _late_thread(outside, go, box)
        go.set()
        th.join(5)
    assert box.get("status") == 200 and box["returned_flag"] is False
    assert any(OUT in h for h in hosts), hosts
    assert netguard.late() == []


def test_a_request_in_flight_when_the_answer_returns_still_gets_its_reply(outside):
    """Already sent = already gone: refusing its reply protects nothing (and the reply cache still fills)."""
    _Handler.delay = 0.6
    box: dict = {}

    def run():
        try:
            with httpx.Client(timeout=5) as c:
                box["status"] = c.get(outside + "/slow").status_code
        except httpx.HTTPError as e:
            box["error"] = e

    with netguard.watch() as hosts:
        ctx = contextvars.copy_context()
        th = threading.Thread(target=lambda: ctx.run(run), daemon=True)
        th.start()
        time.sleep(0.25)  # connected and sent; the reply comes after the answer has been returned
    th.join(5)
    assert box.get("status") == 200, box
    assert any(OUT in h for h in hosts)


def test_work_not_started_by_an_answer_is_not_affected(outside):
    with netguard.watch():
        pass
    go, box = threading.Event(), {}
    th = _late_thread(outside, go, box, copy=False)  # e.g. a warm-up thread: no answer's context
    go.set()
    th.join(5)
    assert box.get("status") == 200 and box["returned_flag"] is False
    with httpx.Client(timeout=5) as c:  # and the main thread after its own answer
        assert c.get(outside + "/after").status_code == 200
    assert netguard.late() == []


def test_a_name_lookup_after_the_answer_is_refused_before_any_dns(outside):
    box: dict = {}
    go = threading.Event()

    def run():
        go.wait(5)
        try:
            socket.getaddrinfo("generativelanguage.googleapis.com", 443)
        except OSError as e:
            box["error"] = e

    with netguard.watch():
        ctx = contextvars.copy_context()
        th = threading.Thread(target=lambda: ctx.run(run), daemon=True)
        th.start()
    go.set()
    th.join(5)
    assert isinstance(box.get("error"), netguard.PrivateNetworkBlocked), box
    assert any("generativelanguage.googleapis.com (name lookup)" == x for x in netguard.late())


def test_private_mode_late_send_is_a_late_refusal_not_a_private_one(outside, monkeypatch):
    go, box = threading.Event(), {}
    with netguard.watch():
        th = _late_thread(outside, go, box)
    monkeypatch.setenv("STOA_PRIVATE", "1")
    go.set()
    th.join(5)
    assert _caused_by_netguard(box.get("error")) and _Handler.hits == []
    assert netguard.late() and netguard.blocked() == []


def test_kill_switch_restores_the_old_behaviour(outside, monkeypatch):
    monkeypatch.setenv("STOA_NETGUARD_LATE", "0")
    go, box = threading.Event(), {}
    with netguard.watch() as hosts:
        th = _late_thread(outside, go, box)
    go.set()
    th.join(5)
    assert box.get("status") == 200 and box["returned_flag"] is False
    assert _Handler.hits == ["GET /late"] and netguard.late() == []
    assert any(OUT in h for h in hosts)  # appended after the answer was read: nobody shows it (the hole)


# ---- end to end: G1's REAL vision reading + stoa.llm's REAL Gemini client, driven by W5's REAL ask_upload ----------

@pytest.fixture()
def gemini_late(outside, monkeypatch, tmp_path):
    """Gemini = the fake outside server; the machine-wide throttle holds the request 1.0 s (another agent's call is
    queued first), the vision wait is 0.3 s: the request would be sent ~0.7 s after the answer was returned."""
    if "netguard" not in ASK.read_text():
        pytest.skip("this tree's web/server/ask_upload.py does not use netguard.watch() yet")
    from PIL import Image

    from stoa import llm
    from stoa.agent import vision

    monkeypatch.setitem(llm.BACKENDS["gemini"], "base_url", outside + "/v1beta/openai/")
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-key")
    monkeypatch.delitem(llm._clients, "gemini", raising=False)
    monkeypatch.setattr(llm, "_nvidia_throttle", lambda backend="nvidia": time.sleep(1.0))
    monkeypatch.setattr(llm, "GEMINI_LOG", tmp_path / "logs" / "gemini_calls.jsonl")
    monkeypatch.setenv("STOA_LLM_CACHE_DIR", str(tmp_path / "llm-cache"))
    monkeypatch.setenv("STOA_VISION_WAIT", "0.3")
    monkeypatch.delenv("STOA_LLM_REPLAY", raising=False)
    monkeypatch.setenv("STOA_GEMINI_TEAM_CAP", "1000")
    png = tmp_path / "plan.png"
    Image.new("RGB", (320, 240), "white").save(png)
    pdf = tmp_path / "sheet.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")

    linger = {"s": 0.0}

    class _Stub:
        def solve(self, path, question):
            base = Answer(value=3, unit="count", confidence=0.6, trace=["stub: local count 3"])
            assert vision.read(str(png), question, base=base) is None  # gave up after the wait: local answer stands
            time.sleep(linger["s"])  # the rest of the answer (other readers) after the vision reading gave up
            return base

    import stoa.solvers as solvers

    monkeypatch.setattr(solvers, "get_solver", lambda name, **kw: _Stub())
    monkeypatch.chdir(ROOT)
    spec = importlib.util.spec_from_file_location("t2_ask_upload_late", ASK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def ask(after_wait: float = 0.0) -> list[str]:
        linger["s"] = after_wait
        out = mod._answer({"abs": str(pdf), "virtual": "upload/t2late/sheet.pdf", "question": "How many doors?",
                           "solver": "dummy", "private": False})
        return [str(t) for t in out.get("trace") or []]

    yield ask
    monkeypatch.delitem(llm._clients, "gemini", raising=False)


def test_e2e_a_gemini_reading_queued_past_its_wait_is_never_sent(gemini_late):
    tr = gemini_late()
    assert any("vision reading failed (no reply within" in t for t in tr), tr
    time.sleep(2.0)  # the throttle releases the request after the answer was returned
    assert _Handler.hits == [], _Handler.hits  # the drawing never went out
    from stoa import llm

    if not hasattr(llm, "_answer_returned"):  # else stoa.llm stops it before the throttle (experiments/t2 P1 patch)
        assert any(OUT in x for x in netguard.late()), netguard.late()
    assert not any(t.startswith("network:") for t in tr)  # and the card's 'stayed on our server' is true


def test_e2e_with_only_the_llm_check_nothing_is_sent_either(gemini_late, monkeypatch):
    """stoa.llm's own check (P1 patch experiments/t2/p1_llm_answer_returned.patch) covers a kept-alive connection,
    which raises no connect event. Skips on a tree without it."""
    from stoa import llm

    if not hasattr(llm, "_answer_returned"):
        pytest.skip("this tree's stoa/llm.py does not check netguard.answer_returned() yet")
    monkeypatch.setattr(netguard, "_after_answer", lambda what: None)  # netguard's own refusal off
    gemini_late()
    time.sleep(2.0)
    assert _Handler.hits == [], _Handler.hits


def test_e2e_control_without_the_fix_the_drawing_goes_out_unseen(gemini_late, monkeypatch):
    """The hole this closes: with STOA_NETGUARD_LATE=0 the same answer says nothing about the network, yet the page
    image reaches 'Google' after the card was written."""
    monkeypatch.setenv("STOA_NETGUARD_LATE", "0")
    from stoa import llm

    if hasattr(llm, "GAVE_UP"):  # with G1 / P1's gave-up hook the request is dropped even earlier
        monkeypatch.setattr(llm, "_gave_up", lambda: False)
    tr = gemini_late()
    assert not any(t.startswith("network:") for t in tr), tr
    for _ in range(40):
        if _Handler.hits:
            break
        time.sleep(0.1)
    assert _Handler.hits == ["POST /v1beta/openai/chat/completions"], _Handler.hits


def test_e2e_a_request_whose_wait_gave_up_is_not_sent_even_while_the_answer_runs(gemini_late):
    """G1 / P1 patches (experiments/t2/g1_vision_gave_up.patch + p1_llm_answer_returned_v2.patch): once the vision wait
    gives up, the queued request is dropped before the send (its reply would be thrown away). Here the answer runs on
    for 2 s after the wait, so netguard's 'after the answer' rule alone would let it out (shown on the card)."""
    from stoa import llm
    from stoa.agent import vision

    if not hasattr(llm, "GAVE_UP") or "gave_up" not in Path(vision.__file__).read_text():
        pytest.skip("this tree has no llm.GAVE_UP / vision gave_up yet")
    tr = gemini_late(after_wait=2.0)
    time.sleep(1.0)
    assert _Handler.hits == [], _Handler.hits
    assert not any(t.startswith("network:") for t in tr), tr


def test_e2e_without_the_gave_up_hook_a_late_request_still_leaves_during_the_answer(gemini_late, monkeypatch):
    """The control for the test above (and the case netguard alone covers by SHOWING it on the card)."""
    from stoa import llm

    if hasattr(llm, "GAVE_UP"):
        monkeypatch.setattr(llm, "_gave_up", lambda: False)
    tr = gemini_late(after_wait=2.0)
    assert _Handler.hits == ["POST /v1beta/openai/chat/completions"], _Handler.hits
    assert any(t.startswith("network: this answer reached ") and OUT in t for t in tr), tr
