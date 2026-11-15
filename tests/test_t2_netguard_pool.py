"""T2 cycle 3: a kept-alive connection (an HTTP client's pool, e.g. stoa.llm's cached OpenAI clients: keepalive 5 s)
raises no audit event when it is reused. netguard must (1) close such outside connections before a private request can
send on them and (2) count a reused connection as a destination of the request that sent on it.

The test treats 127.0.0.2 as 'outside' (netguard._local_ip patched), runs a real HTTP server there and talks to it with
the same client library the OpenAI SDK uses (httpx2), so the pool and the audit events are the real ones."""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

from stoa import netguard

httpx = pytest.importorskip("httpx2")
OUT = "127.0.0.2"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture()
def outside_server(monkeypatch):
    try:
        srv = http.server.ThreadingHTTPServer((OUT, 0), _Handler)
    except OSError as e:  # pragma: no cover (no 127.0.0.2 on this machine)
        pytest.skip(f"cannot bind {OUT}: {e}")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    real = netguard._local_ip
    monkeypatch.setattr(netguard, "_local_ip", lambda h: False if h.split("%")[0] == OUT else real(h))
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    assert netguard.install()
    netguard.blocked(clear=True)
    netguard.closed(clear=True)
    _Handler.hits.clear()
    try:
        yield f"http://{OUT}:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        netguard._forget_all()


def test_a_private_request_cannot_reuse_a_kept_alive_outside_connection(outside_server, monkeypatch):
    with httpx.Client(timeout=5) as client:
        assert client.get(outside_server + "/first").text == "ok"  # private off: allowed, connection pooled
        monkeypatch.setenv("STOA_PRIVATE", "1")
        with pytest.raises(httpx.HTTPError) as ei:
            client.get(outside_server + "/second")
        chain, e = [], ei.value
        while e is not None:
            chain.append(type(e).__name__)
            e = e.__cause__ or e.__context__
        assert "PrivateNetworkBlocked" in chain, chain
    assert _Handler.hits == ["/first"], "the private request reached the outside server over the pooled connection"
    assert any("kept-alive" in b for b in netguard.closed()), netguard.closed()
    assert not any("kept-alive" in b for b in netguard.blocked())  # a closure is not a refusal (W5's trace line)


def test_a_reused_connection_counts_as_a_destination(outside_server):
    with httpx.Client(timeout=5) as client:
        with netguard.watch() as first:
            client.get(outside_server + "/a")
        with netguard.watch() as idle:  # the pooled connection stays open but sends nothing
            pass
        with netguard.watch() as reused:  # no new connect / lookup event: the pool reuses the socket
            client.get(outside_server + "/b")
    assert first and any(OUT in h for h in first), first
    assert idle == [], idle
    assert reused and any(OUT in h for h in reused), reused
    assert _Handler.hits == ["/a", "/b"]


def test_local_connections_are_not_tracked_or_closed(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    assert netguard.install()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname())
    try:
        assert c.fileno() not in netguard._tracked
        monkeypatch.setenv("STOA_PRIVATE", "1")
        open(__file__).close()  # any audited event in private mode
        conn, _ = srv.accept()
        c.sendall(b"x")
        assert conn.recv(1) == b"x"  # our own connection still works
        conn.close()
    finally:
        c.close()
        srv.close()


def test_pipeline_private_flag_closes_the_pool_at_its_first_audited_event(outside_server):
    """G1's per-request flag (Pipeline(private=1) sets stoa.agent.private._CTX) raises no event of its own; the
    request's first audited step (here: reading the drawing) closes the kept-alive connection."""
    priv = pytest.importorskip("stoa.agent.private")
    with httpx.Client(timeout=5) as client:
        client.get(outside_server + "/first")
        tok = priv._CTX.set(True)
        try:
            open(__file__, "rb").close()  # the pipeline opens the drawing before any model call
            with pytest.raises(httpx.HTTPError):
                client.get(outside_server + "/second")
        finally:
            priv._CTX.reset(tok)
        assert client.get(outside_server + "/third").text == "ok"  # private off again: a new connection is fine
    assert _Handler.hits == ["/first", "/third"]


def test_eu_only_private_keeps_its_verda_connection(outside_server, monkeypatch):
    """STOA_EU_ONLY=1: Verda stays allowed, so its kept-alive connection must not be shut down by private mode (it
    may be the private request's own call in flight)."""
    monkeypatch.setenv("VERDA_INFERENCE_BASE_URL", outside_server + "/v1")
    monkeypatch.setattr(netguard, "_eu_ips", {})
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    with httpx.Client(timeout=5) as client:
        assert client.get(outside_server + "/first").text == "ok"
        monkeypatch.setenv("STOA_PRIVATE", "1")
        open(__file__, "rb").close()
        assert client.get(outside_server + "/second").text == "ok"
    assert _Handler.hits == ["/first", "/second"]
    assert not any("kept-alive" in b for b in netguard.closed())


def test_a_private_request_closes_the_pool_once_not_a_concurrent_visitors_new_connection(outside_server):
    """API threads: a private request (G1's context variable, its own copied context) closes the connections that
    were open when it started; a non-private request in another thread that connects meanwhile keeps its connection
    (closing at every later event would break that visitor's hosted call over and over)."""
    import contextvars

    priv = pytest.importorskip("stoa.agent.private")
    other = httpx.Client(timeout=5)
    try:
        other.get(outside_server + "/before")  # open before the private request: must be closed

        def private_request(step):
            priv._CTX.set(True)
            open(__file__, "rb").close()  # first audited event: closes what is open now
            step.wait(5)  # meanwhile another visitor's request connects (below)
            for _ in range(3):
                open(__file__, "rb").close()  # later events of the same private request

        step = threading.Event()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(private_request, step))
        t.start()
        for _ in range(250):  # wait until the private request's first event closed the old connection
            if any("kept-alive" in b for b in netguard.closed()):
                break
            threading.Event().wait(0.02)
        assert any("kept-alive" in b for b in netguard.closed())
        assert other.get(outside_server + "/during").text == "ok"  # non-private thread: reconnects, allowed
        n_open = len(netguard._tracked)
        step.set()
        t.join(5)
        assert len(netguard._tracked) == n_open >= 1  # not closed again by the private request's later events
        assert other.get(outside_server + "/after").text == "ok"
    finally:
        other.close()
    assert _Handler.hits == ["/before", "/during", "/after"]
