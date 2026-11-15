"""T2 cycle 3: an upload answer says which outside hosts it really reached, measured at the socket (W5 wired
stoa.netguard into web/server/ask_upload.py, night-W5 8205a62a: 'network: this answer reached <hosts>' and
'private mode: N outside connection(s) refused (...)'). Skips on a tree whose ask_upload.py does not use netguard yet.

A stub solver talks to a real HTTP server on 127.0.0.2 (treated as outside: netguard._local_ip patched) with the
client library the OpenAI SDK uses (httpx2), so connection pooling and the audit events are the real ones."""

from __future__ import annotations

import http.server
import importlib.util
import os
import threading
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

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class _Stub:
    client = None
    url = ""

    def __init__(self, reach: bool):
        self.reach = reach

    def solve(self, path, question):
        if self.reach:
            try:
                _Stub.client.get(_Stub.url + "/ask")
            except httpx.HTTPError:
                pass
        return Answer(value=1, unit="count", confidence=0.5, trace=["stub"])


@pytest.fixture()
def env(monkeypatch, tmp_path):
    if "netguard" not in ASK.read_text():
        pytest.skip("this tree's web/server/ask_upload.py does not report netguard's measurement yet (night-W5 8205a62a)")
    try:
        srv = http.server.ThreadingHTTPServer((OUT, 0), _Handler)
    except OSError as e:  # pragma: no cover
        pytest.skip(f"cannot bind {OUT}: {e}")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = netguard._local_ip
    monkeypatch.setattr(netguard, "_local_ip", lambda h: False if h.split("%")[0] == OUT else real(h))
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    assert netguard.install()
    netguard.blocked(clear=True)
    netguard.closed(clear=True)
    _Handler.hits.clear()
    spec = importlib.util.spec_from_file_location("t2_ask_upload_net", ASK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import stoa.solvers as solvers

    reach = {"on": False}
    monkeypatch.setattr(solvers, "get_solver", lambda name, **kw: _Stub(reach["on"]))
    monkeypatch.chdir(ROOT)
    p = tmp_path / "sheet.pdf"
    p.write_bytes(b"%PDF-1.4\n%stub\n")
    _Stub.client, _Stub.url = httpx.Client(timeout=5), f"http://{OUT}:{srv.server_address[1]}"

    def ask(reach_out: bool, private: bool) -> list[str]:
        reach["on"] = reach_out
        out = mod._answer({"abs": str(p), "virtual": "upload/t2net/sheet.pdf", "question": "How many?",
                           "solver": "dummy", "private": private})
        return [str(t) for t in out.get("trace") or []]

    try:
        yield ask
    finally:
        _Stub.client.close()
        srv.shutdown()
        srv.server_close()
        netguard._forget_all()


def _line(trace: list[str], start: str) -> str | None:
    return next((t for t in trace if t.startswith(start)), None)


def test_an_outside_connection_is_listed_on_the_answer(env):
    tr = env(True, False)
    line = _line(tr, "network: this answer reached ")
    assert line and OUT in line, tr
    assert _line(tr, "private mode:") is None


def test_a_reused_connection_is_still_listed(env):
    env(True, False)  # opens the pooled connection
    tr = env(True, False)  # reuses it: no new connect / lookup event
    line = _line(tr, "network: this answer reached ")
    assert line and OUT in line, tr
    assert _Handler.hits == ["/ask", "/ask"]


def test_a_local_answer_lists_nothing(env):
    env(True, False)
    tr = env(False, False)  # the previous answer's open connection sends nothing now
    assert _line(tr, "network:") is None, tr


def test_private_answer_after_a_hosted_one_reaches_nothing_and_refuses_nothing(env):
    """The pooled connection the previous (non-private) answer left open is shut down when private mode starts: it is
    not reported as a refusal (nothing in the private answer tried to go out) and nothing is reached."""
    env(True, False)
    tr = env(False, True)
    assert _line(tr, "network:") is None and _line(tr, "private mode:") is None, tr
    assert any(OUT in c for c in netguard.closed())


def test_private_answer_that_tries_is_refused_and_reaches_nothing(env):
    env(True, False)
    tr = env(True, True)  # tries the pooled connection: closed at private start, the reconnect is refused
    assert _line(tr, "network:") is None, tr
    line = _line(tr, "private mode: ")
    assert line and "refused" in line and OUT in line, tr
    assert _Handler.hits == ["/ask"]  # the private answer's request never reached the server


# ---- T2 00:4x (captain 00:31, audit RECHECK-0026 new problem #1): the network is the truth for data_used / api_calls --

def _data_used(trace: list[str]) -> str | None:
    return next((t for t in trace if t.startswith("data_used:")), None)


def test_a_hosted_host_seen_on_the_network_is_added_to_data_used_and_api_calls(env):
    """A Gemini request whose reply missed the budget leaves no step credit and no cost: the card said 'Stayed on our
    server'. Whatever the network saw is now in data_used and api_calls (here an unnamed outside address -> 'hosted')."""
    if "_network_is_the_truth" not in ASK.read_text():
        pytest.skip("this tree's ask_upload.py does not add network-seen providers to data_used yet")
    tr = env(True, False)
    assert _line(tr, "network: this answer reached ") and OUT in _line(tr, "network: this answer reached ")
    du = _data_used(tr)
    assert du and "hosted" in du.split(":", 1)[1], tr


def test_a_local_or_private_answer_keeps_its_data_used(env):
    if "_network_is_the_truth" not in ASK.read_text():
        pytest.skip("this tree's ask_upload.py does not add network-seen providers to data_used yet")
    env(True, False)
    for tr in (env(False, False), env(True, True)):  # nothing reached / everything refused
        du = _data_used(tr)
        assert du is None or "hosted" not in du, tr


def test_network_hosts_map_to_providers():
    import importlib.util

    if "_net_providers" not in ASK.read_text():
        pytest.skip("this tree's ask_upload.py has no _net_providers")
    spec = importlib.util.spec_from_file_location("t2_ask_upload_map", ASK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    m = mod._net_providers
    assert m(["142.250.74.10:443", "generativelanguage.googleapis.com"]) == ["gemini"]  # the audit's M-101A trace
    assert m(["integrate.api.nvidia.com", "generativelanguage.googleapis.com (142.250.74.10:443)"]) == ["nvidia", "gemini"]
    assert m(["inference.verda.com"]) == ["verda"]
    assert m(["127.0.0.1:8148 (forwards outside: DET_REMOTE_URL -> ssh tunnel)"]) == ["relay"]
    assert m(["127.0.0.1:8148 (forwards outside: ssh tunnel)", "203.0.113.9:443"]) == ["relay", "hosted"]
    assert m(["203.0.113.9:443"]) == ["hosted"] and m(["example-gpu.net"]) == ["hosted"]
    a = Answer(value=1, unit="count", confidence=0.5, trace=["x", "data_used: local"], data_used=["local"])
    mod._network_is_the_truth(a, ["142.250.74.10:443", "generativelanguage.googleapis.com"])
    assert a.trace == ["x", "data_used: local, gemini"] and a.data_used == ["local", "gemini"] and a.cost.api_calls == 1
    b = Answer(value=1, unit="count", confidence=0.5, trace=["data_used: local, gemini"])
    b.cost.api_calls = 3
    mod._network_is_the_truth(b, ["generativelanguage.googleapis.com"])
    assert b.trace == ["data_used: local, gemini"] and b.cost.api_calls == 3  # nothing doubled, nothing lowered
    c = Answer(value=1, unit="count", confidence=0.5, trace=[])
    mod._network_is_the_truth(c, ["127.0.0.1:8148 (forwards outside: ssh tunnel)"])
    assert c.trace == ["data_used: local, relay"] and c.cost.api_calls == 0  # a forwarding detector is not an API call
