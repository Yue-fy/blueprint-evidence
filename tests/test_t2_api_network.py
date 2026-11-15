"""T2 cycle 7: the API's /ask (dataset files; Gemini + Nemotron are ON there by default) says what the network layer
saw, like the web's upload worker: a hosted request whose reply missed its wait was still SENT, and the card's 'where
your data went' must say so; work that runs on after the answer may not send anything (netguard.late).
Skips until stoa/api.py wraps the solve in netguard.watch() (patch: experiments/t2/s0_api_network_truth.patch).

The 'outside' host is a real HTTP server on 127.0.0.2 (netguard._local_ip patched); a stub solver reaches it."""

from __future__ import annotations

import contextvars
import http.server
import threading
import time
from pathlib import Path

import pytest

from stoa import netguard
from stoa.contract import Answer

httpx = pytest.importorskip("httpx2")
pytest.importorskip("fastapi")
API = Path(__file__).resolve().parents[1] / "stoa" / "api.py"
OUT = "127.0.0.2"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class _Stub:
    url = ""
    mode = "local"

    def solve(self, path, question):
        a = Answer(value=4, unit="count", confidence=0.4, trace=["stub", "data_used: local"], data_used=["local"])
        if _Stub.mode == "reach":
            with httpx.Client(timeout=5) as c:
                c.get(_Stub.url + "/ask")
        elif _Stub.mode == "late":  # a background step of this answer that sends only after it was returned
            ctx = contextvars.copy_context()

            def later():
                time.sleep(0.4)
                try:
                    with httpx.Client(timeout=5) as c:
                        c.get(_Stub.url + "/late")
                except httpx.HTTPError:
                    pass

            threading.Thread(target=lambda: ctx.run(later), daemon=True).start()
        return a


@pytest.fixture()
def client(monkeypatch):
    if "netguard" not in API.read_text():
        pytest.skip("this tree's stoa/api.py does not use netguard.watch() yet (experiments/t2/s0_api_network_truth.patch)")
    from fastapi.testclient import TestClient

    import stoa.api as api

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
    netguard.late(clear=True)
    _Handler.hits.clear()
    _Stub.url = f"http://{OUT}:{srv.server_address[1]}"
    monkeypatch.setattr(api, "_solver", lambda name, kwargs: _Stub())
    monkeypatch.setattr(api, "_safe_rel", lambda p: p)
    api._ANSWERS.clear()
    try:
        yield TestClient(api.app)
    finally:
        srv.shutdown()
        srv.server_close()
        netguard._forget_all()
        netguard.late(clear=True)
        api._ANSWERS.clear()


def _ask(c, mode: str, q: str) -> dict:
    _Stub.mode = mode
    r = c.post("/ask", json={"path": "data/x/sheet.pdf", "question": q})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_answer_that_reached_outside_says_so(client):
    out = _ask(client, "reach", "How many doors? (reach)")
    tr = [str(t) for t in out["trace"]]
    assert any(t.startswith("network: this answer reached ") and OUT in t for t in tr), tr
    assert "data_used: local, hosted" in tr and "hosted" in out["data_used"], (tr, out["data_used"])
    assert out["cost"]["api_calls"] >= 1
    assert any(t.startswith("data_used says local but the network saw") for t in tr)


def test_api_local_answer_unchanged(client):
    out = _ask(client, "local", "How many doors? (local)")
    tr = [str(t) for t in out["trace"]]
    assert not any(t.startswith("network:") for t in tr) and out["data_used"] == ["local"], tr
    assert out["cost"]["api_calls"] == 0


def test_api_work_after_the_answer_sends_nothing(client):
    out = _ask(client, "late", "How many doors? (late)")
    assert not any(str(t).startswith("network:") for t in out["trace"])
    time.sleep(1.0)
    assert _Handler.hits == []
    assert any(OUT in x for x in netguard.late()), netguard.late()


def test_providers_match_the_upload_workers_mapping():
    import importlib.util

    ask = Path(__file__).resolve().parents[1] / "web" / "server" / "ask_upload.py"
    if not hasattr(netguard, "providers") or "_net_providers" not in ask.read_text():
        pytest.skip("no netguard.providers / ask_upload._net_providers on this tree")
    spec = importlib.util.spec_from_file_location("t2_ask_upload_prov", ask)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for seen in (["142.250.74.10:443", "generativelanguage.googleapis.com"], ["integrate.api.nvidia.com"],
                 ["inference.verda.com"], ["127.0.0.1:8148 (forwards outside: ssh tunnel)", "203.0.113.9:443"],
                 ["203.0.113.9:443"], ["example-gpu.net"]):
        assert netguard.providers(seen) == mod._net_providers(seen), seen
