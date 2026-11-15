"""T2 cycle 6: 127.0.0.1 is not always 'ours'. An ssh tunnel listening on a loopback port carries what is sent there to
a machine elsewhere, and a local service can pass its work on (V1's det_server with DET_REMOTE_URL posts each page
image to a GPU twin on a rented Verda instance through such a tunnel: live on :8145 / :8148 tonight). netguard must
treat a connect to such a port like an outside connect: refused with private mode on, a measured destination with it
off, and a kept-alive connection to it closed when a private request starts.

The /proc scan is tested on a fake /proc tree (ssh processes need an outside peer, which a test cannot make), and end to
end with a REAL local process whose environment sets DET_REMOTE_URL to an outside host: the real scan must find its
port, and a real HTTP request to it from this process must be refused in private mode."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request

import pytest

from stoa import netguard

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the relay scan reads Linux /proc")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_NETGUARD_RELAYS", raising=False)
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    monkeypatch.delenv("STOA_EU_RELAY_PORTS", raising=False)
    netguard._relay_cache.update(t=None, ports={})
    netguard.blocked(clear=True)
    netguard.closed(clear=True)
    yield
    netguard._relay_cache.update(t=None, ports={})
    netguard._forget_all()
    netguard.blocked(clear=True)
    netguard.closed(clear=True)


# ---- /proc parsing ------------------------------------------------------------------------------------------------

def _hex(ip: str, port: int) -> str:
    a = ipaddress.ip_address(ip)
    b = a.packed
    if a.version == 4:
        h = b[::-1].hex()
    else:
        h = b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4)).hex()
    return f"{h.upper()}:{port:04X}"


def test_hexaddr_reads_proc_net_tcp_addresses():
    assert netguard._hexaddr("0100007F:1F90") == ("127.0.0.1", 8080)
    assert netguard._hexaddr("00000000000000000000000001000000:0050") == ("::1", 80)
    assert netguard._hexaddr("0000000000000000FFFF00000100007F:1F90") == ("127.0.0.1", 8080)  # v4-mapped
    assert netguard._hexaddr(_hex("203.0.113.7", 22)) == ("203.0.113.7", 22)


def _fake_proc(root, procs, sockets):
    """procs: pid -> (comm, environ dict | None, [inode, ...]); sockets: inode -> (state, local, remote, v6)."""
    (root / "net").mkdir(parents=True)
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = {False: [head], True: [head]}
    for n, (ino, (st, loc, rem, v6)) in enumerate(sockets.items()):
        rows[v6].append(f"   {n}: {_hex(*loc)} {_hex(*rem)} {st} 00000000:00000000 00:00000000 00000000  1001 "
                        f"       0 {ino} 1 0000000000000000 100 0 0 10 0\n")
    (root / "net" / "tcp").write_text("".join(rows[False]))
    (root / "net" / "tcp6").write_text("".join(rows[True]))
    for pid, (comm, env, inos) in procs.items():
        d = root / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        if env is not None:
            (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
        for fd, ino in enumerate(inos, start=3):
            os.symlink(f"socket:[{ino}]", d / "fd" / str(fd))
        os.symlink("/dev/null", d / "fd" / "0")


def test_scan_finds_tunnels_and_services_that_forward_through_them(tmp_path):
    L, E = "0A", "01"
    any4, any6 = ("0.0.0.0", 0), ("::", 0)
    sockets = {
        1001: (L, ("127.0.0.1", 9101), any4, False), 1002: (E, ("10.0.0.2", 40000), ("203.0.113.7", 22), False),
        1011: (L, ("127.0.0.1", 9102), any4, False), 1012: (E, ("10.0.0.2", 40001), ("10.1.2.3", 22), False),
        1021: (L, ("0.0.0.0", 9103), any4, False),
        1031: (L, ("127.0.0.1", 9104), any4, False),
        1041: (L, ("::1", 9105), any6, True),
        1051: (L, ("127.0.0.1", 9106), any4, False),
        1061: (L, ("10.0.0.5", 9107), any4, False), 1062: (E, ("10.0.0.2", 40002), ("198.51.100.9", 22), False),
        1071: (L, ("127.0.0.1", 9108), any4, False), 1072: (E, ("10.0.0.2", 40003), ("198.51.100.9", 22), False),
    }
    procs = {
        100: ("ssh", None, [1001, 1002]),  # ssh -L 127.0.0.1:9101 -> a machine outside: relay
        101: ("ssh", None, [1011, 1012]),  # ssh -L to a machine on our own network: not a relay
        102: ("python3", {"DET_REMOTE_URL": "http://127.0.0.1:9101", "PATH": "/bin"}, [1021]),  # det_server + tunnel
        # remote = a local port, no tunnel; its other URLs (an LLM base URL outside) are not a forward of this port
        103: ("python3", {"DET_REMOTE_URL": "http://127.0.0.1:9999", "LLM2_BASE_URL": "https://integrate.api.nvidia.com/v1"},
              [1031]),
        104: ("python3", {"OCR_REMOTE_URL": "https://gpu.example.org/v1"}, [1041]),  # remote = an outside name
        105: ("python3", {"LLM2_BASE_URL": "https://integrate.api.nvidia.com/v1"}, [1051]),  # e.g. our API: not flagged
        106: ("ssh", None, [1061, 1062]),  # an outside tunnel listening on a LAN address only: no loopback connect
        107: ("autossh", None, [1071, 1072]),
    }
    _fake_proc(tmp_path, procs, sockets)
    got = netguard._scan_relays(str(tmp_path))
    assert got == {
        9101: "forwards outside: ssh tunnel",
        9103: "forwards outside: DET_REMOTE_URL -> ssh tunnel",
        9105: "forwards outside: OCR_REMOTE_URL -> gpu.example.org",
        9108: "forwards outside: ssh tunnel",
    }


def test_scan_survives_an_unreadable_or_odd_proc(tmp_path):
    assert netguard._scan_relays(str(tmp_path / "missing")) == {}
    _fake_proc(tmp_path, {200: ("python3", {"X_REMOTE_URL": "::not a url::"}, [7])},
               {7: ("0A", ("127.0.0.1", 9200), ("0.0.0.0", 0), False)})
    (tmp_path / "net" / "tcp").write_text((tmp_path / "net" / "tcp").read_text() + "garbage line\n   9: zz:zz\n")
    assert netguard._scan_relays(str(tmp_path)) == {}


# ---- the hook, end to end with a real local process ---------------------------------------------------------------

_SERVER = textwrap.dedent("""
    import socket, sys, threading
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(16)
    print(s.getsockname()[1], flush=True)
    def serve(c):
        buf = b""
        while b"\\r\\n\\r\\n" not in buf:
            d = c.recv(4096)
            if not d:
                c.close(); return
            buf += d
        c.sendall(b"HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nok"); c.close()
    while True:
        c, _ = s.accept(); threading.Thread(target=serve, args=(c,), daemon=True).start()
""")


@pytest.fixture()
def local_service():
    """Start a real local HTTP service; forwards=True gives it DET_REMOTE_URL to an outside host (like a det_server
    set to offload). Returns its port."""
    procs = []

    def start(forwards: bool) -> int:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if forwards:
            env["DET_REMOTE_URL"] = "https://gpu.example.org"
        p = subprocess.Popen([sys.executable, "-I", "-c", _SERVER], stdout=subprocess.PIPE, env=env, text=True)
        procs.append(p)
        port = int(p.stdout.readline())
        return port

    yield start
    for p in procs:
        p.kill()
        p.wait(5)


def _get(port: int) -> bytes:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        return r.read()


@linux
def test_private_mode_refuses_a_local_port_that_forwards_outside(local_service, monkeypatch):
    if not netguard._installed:
        pytest.skip("netguard is not installed in this process (STOA_NETGUARD=0)")
    fwd, plain = local_service(True), local_service(False)
    ports = netguard.relays(refresh=True)
    assert ports.get(fwd) == "forwards outside: DET_REMOTE_URL -> gpu.example.org"
    assert plain not in ports
    assert netguard.relay_for_url(f"http://127.0.0.1:{fwd}/detect") == ports[fwd]
    assert netguard.relay_for_url(f"http://localhost:{fwd}") == ports[fwd]
    assert netguard.relay_for_url(f"http://127.0.0.1:{plain}") is None

    monkeypatch.setenv("STOA_PRIVATE", "1")
    with pytest.raises(urllib.error.URLError) as e:
        _get(fwd)
    assert isinstance(e.value.reason, netguard.PrivateNetworkBlocked)
    assert netguard.blocked() == [f"127.0.0.1:{fwd} (forwards outside: DET_REMOTE_URL -> gpu.example.org)"]
    assert _get(plain) == b"ok"  # a plain local service is still ours


@linux
def test_private_off_counts_it_as_a_destination_and_never_breaks_it(local_service):
    if not netguard._installed:
        pytest.skip("netguard is not installed in this process (STOA_NETGUARD=0)")
    fwd, plain = local_service(True), local_service(False)
    netguard.relays(refresh=True)
    with netguard.watch() as hosts:
        assert _get(fwd) == b"ok"
        assert _get(plain) == b"ok"
    assert hosts == [f"127.0.0.1:{fwd} (forwards outside: DET_REMOTE_URL -> gpu.example.org)"]
    assert netguard.blocked() == []


@linux
def test_a_kept_alive_connection_to_it_is_closed_when_private_starts(local_service, monkeypatch):
    if not netguard._installed:
        pytest.skip("netguard is not installed in this process (STOA_NETGUARD=0)")
    fwd, plain = local_service(True), local_service(False)
    netguard.relays(refresh=True)
    a = socket.create_connection(("127.0.0.1", fwd), timeout=5)  # opened by a non-private request, left in a pool
    b = socket.create_connection(("127.0.0.1", plain), timeout=5)
    try:
        assert a.fileno() in netguard._relayed and b.fileno() not in netguard._tracked
        monkeypatch.setenv("STOA_PRIVATE", "1")  # the upload worker turning private on
        assert any(f"127.0.0.1:{fwd} (forwards outside" in c for c in netguard.closed())
        a.settimeout(2)
        with pytest.raises(OSError):
            for _ in range(3):  # the shut-down connection cannot carry a request any more
                a.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if a.recv(10) == b"":
                    raise OSError("closed")
        b.sendall(b"GET / HTTP/1.0\r\n\r\n")  # the plain local one is untouched
        assert b.recv(64).startswith(b"HTTP/1.0 200")
    finally:
        a.close()
        b.close()


def test_switch_off_and_cache(monkeypatch):
    monkeypatch.setenv("STOA_NETGUARD_RELAYS", "0")
    assert netguard.relays(refresh=True) == {}
    monkeypatch.delenv("STOA_NETGUARD_RELAYS")
    calls = []
    monkeypatch.setattr(netguard, "_scan_relays", lambda root="/proc": calls.append(1) or {1234: "x"})
    netguard._relay_cache.update(t=None, ports={})
    assert netguard.relays() == {1234: "x"} and netguard.relays() == {1234: "x"}
    assert len(calls) == 1  # cached for STOA_NETGUARD_RELAY_TTL seconds
    monkeypatch.setenv("STOA_NETGUARD_RELAY_TTL", "0")
    time.sleep(0.01)
    netguard.relays()
    assert len(calls) == 2


@linux
def test_eu_relay_ports_pass_only_with_eu_mode(local_service, monkeypatch):
    """The operator may state that a forward ends at our own EU instance (STOA_EU_RELAY_PORTS): W5's 'EU' option
    (STOA_EU_ONLY=1) then allows it like Verda's hosted models; strict private mode still refuses it."""
    if not netguard._installed:
        pytest.skip("netguard is not installed in this process (STOA_NETGUARD=0)")
    fwd = local_service(True)
    netguard.relays(refresh=True)
    monkeypatch.setenv("STOA_EU_RELAY_PORTS", f"1, {fwd}")
    monkeypatch.setenv("STOA_PRIVATE", "1")
    with pytest.raises(urllib.error.URLError) as e:
        _get(fwd)  # private, not EU: refused although the port is listed
    assert isinstance(e.value.reason, netguard.PrivateNetworkBlocked) and len(netguard.blocked(clear=True)) == 1
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    with netguard.watch() as hosts:
        assert _get(fwd) == b"ok"
    assert hosts == [f"127.0.0.1:{fwd} (forwards outside: DET_REMOTE_URL -> gpu.example.org)"]  # still shown
    monkeypatch.setenv("STOA_EU_RELAY_PORTS", "1")
    with pytest.raises(urllib.error.URLError) as e:
        _get(fwd)  # EU mode, port not listed: refused
    assert isinstance(e.value.reason, netguard.PrivateNetworkBlocked)


@linux
def test_private_mode_records_a_forwarding_service_it_keeps_off(local_service, monkeypatch):
    """T2 cycle 7 (H 01:30): stoa.llm.check_private_url refuses the door detector :8145 (it offloads page images to a
    rented GPU) BEFORE any socket, so netguard saw nothing and the answer never said why the door finder was missing.
    In private mode relay_for_url records it in blocked() ('...; not called'); with private mode off it records nothing."""
    fwd, plain = local_service(True), local_service(False)
    netguard.relays(refresh=True)
    netguard.blocked(clear=True)
    assert netguard.relay_for_url(f"http://127.0.0.1:{fwd}/detect")
    assert netguard.blocked() == []  # private off: just a question
    monkeypatch.setenv("STOA_PRIVATE", "1")
    assert netguard.relay_for_url(f"http://127.0.0.1:{plain}") is None
    assert netguard.blocked() == []
    why = netguard.relay_for_url(f"http://127.0.0.1:{fwd}/detect")
    assert netguard.relay_for_url(f"http://127.0.0.1:{fwd}/detect") == why  # asked twice: listed once
    assert netguard.blocked() == [f"127.0.0.1:{fwd} ({why}; not called)"]
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    monkeypatch.setenv("STOA_EU_RELAY_PORTS", str(fwd))  # the operator's own EU instance: allowed in EU mode
    netguard.blocked(clear=True)
    netguard.relay_for_url(f"http://127.0.0.1:{fwd}/detect")
    assert netguard.blocked() == []


@linux
def test_private_answer_names_the_reader_it_kept_off(local_service, monkeypatch, tmp_path):
    """End to end through W5's real ask_upload (patch experiments/t2/w5_private_kept_off.patch; skips without it): a
    Private answer whose door finder forwards outside gets a plain 'missing' line saying so (H 01:30: the AFCEC door
    count fell back to a 12 @0.09 guess that only said 'count by hand')."""
    import importlib.util
    import os as _os
    from pathlib import Path

    from stoa import llm
    from stoa.contract import Answer

    ask = Path(_os.environ.get("T2_ASK_UPLOAD_PATH") or Path(__file__).resolve().parents[1] / "web" / "server" / "ask_upload.py")
    if "kept_off" not in ask.read_text() or not hasattr(llm, "check_private_url"):
        pytest.skip("this tree's ask_upload.py does not add netguard.kept_off() to missing yet")
    import inspect

    if "relay_for_url" not in inspect.getsource(llm.check_private_url):
        pytest.skip("this tree's stoa.llm.check_private_url does not ask netguard about forwarding ports (P1 bd2ae570)")
    fwd = local_service(True)
    netguard.relays(refresh=True)
    url = f"http://127.0.0.1:{fwd}/detect"

    class _Stub:
        def solve(self, path, question):
            try:
                llm.check_private_url(url, "door detector")
                said = "called"
            except Exception as e:  # noqa: BLE001 — ExternalCallBlocked: the reader abstains
                said = type(e).__name__
            return Answer(value=12, unit="count", confidence=0.09, trace=[f"stub detector: {said}"],
                          missing=["located none of them; count by hand"])

    import stoa.solvers as solvers

    monkeypatch.setattr(solvers, "get_solver", lambda name, **kw: _Stub())
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    spec = importlib.util.spec_from_file_location("t2_ask_upload_keptoff", ask)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pdf = tmp_path / "sheet.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%stub\n")
    out = mod._answer({"abs": str(pdf), "virtual": "upload/t2keep/sheet.pdf", "question": "How many doors?",
                       "solver": "dummy", "private": True})
    assert "stub detector: ExternalCallBlocked" in out["trace"], out["trace"]
    miss = [str(m) for m in out.get("missing") or []]
    assert any(m.startswith("Private mode kept the door and window finder off") and f"127.0.0.1:{fwd}" in m
               for m in miss), miss
    assert miss[0] == "located none of them; count by hand"  # the reader's own words stay first
    out2 = mod._answer({"abs": str(pdf), "virtual": "upload/t2keep/sheet.pdf", "question": "How many doors? (public)",
                        "solver": "dummy", "private": False})
    assert not any("Private mode kept" in str(m) for m in out2.get("missing") or [])
