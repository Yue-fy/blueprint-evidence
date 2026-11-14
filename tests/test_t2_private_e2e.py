"""T2 (audit 19:51 fix #3): a PRIVATE request makes zero outbound connections, end to end.

Hosted clients are stubbed: the Gemini / NVIDIA / Verda base URLs point at TEST-NET addresses (RFC 5737, never
routed), keys are fake, and a recorder audit hook refuses (and logs) every connection outside this machine, so
nothing can leave even when a test fails. The upload path (web/server/ask_upload.py answer(), what the Vite
middleware runs for /api/ask on an uploaded file, including the automatic scale question) is asked with every
hosted switch forced ON (request solver_args gemini=1 verify=1, STOA_MODE=api, STOA_TEXT_BACKEND=gemini). Private
must reach no hosted client and open no outside connection; the same request NOT private is the control that shows
the recorder would have seen a leak.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

from stoa import netguard

ROOT = Path(__file__).resolve().parents[1]
TEST_NET = {"gemini": "https://203.0.113.10/v1beta/openai/", "nvidia": "https://203.0.113.11/v1",
            "verda": "https://203.0.113.12/v1", "api": "https://203.0.113.13/v1"}
SEEN: list[str] = []  # outside connections that got past netguard (each refused by the recorder below)
RECORDING = {"on": False, "abort": False}


class _FirstLeak(BaseException):
    """Control test: stop the whole request at its first outside connection (HTTP clients catch Exception only)."""


def _recorder(event, args):
    if not RECORDING["on"] or event != "socket.connect":
        return
    sock, addr = args[0], args[1]
    if getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6) and isinstance(addr, tuple):
        if not netguard._allowed_host(str(addr[0])) or str(addr[0]).startswith("203.0.113."):
            SEEN.append(f"{addr[0]}:{addr[1]}")
            if RECORDING["abort"]:
                raise _FirstLeak(SEEN[-1])
            raise ConnectionRefusedError("test recorder: outside connection refused")


sys.addaudithook(_recorder)  # after netguard's hook: it only sees what netguard let through


@pytest.fixture()
def hosted_stubs(monkeypatch, tmp_path):
    """Every hosted backend -> TEST-NET + fake key; OpenAI client constructions recorded; Gemini log in tmp."""
    import openai

    from stoa import llm

    made: list[str] = []
    real = openai.OpenAI

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            made.append(str(kw.get("base_url", "")))
            super().__init__(*a, **kw)

    monkeypatch.setattr(openai, "OpenAI", Recording)
    for name, url in TEST_NET.items():
        if name in llm.BACKENDS:
            monkeypatch.setitem(llm.BACKENDS, name, {**llm.BACKENDS[name], "base_url": url, "timeout": 3})
    monkeypatch.setattr(llm, "_clients", {})
    for k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "VERDA_API_KEY"):
        monkeypatch.setenv(k, "test-key-not-real")
    if hasattr(llm, "GEMINI_LOG"):
        monkeypatch.setattr(llm, "GEMINI_LOG", tmp_path / "gemini_calls.jsonl")
    try:
        from stoa.agent import escalate

        monkeypatch.setattr(escalate, "TEAM_DAY_CAP", 10 ** 9)  # the cap must not hide a hosted step
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setenv("STOA_LLM_CACHE", "0")
    monkeypatch.setenv("STOA_API_GEMINI", "1")
    monkeypatch.setenv("STOA_API_VERIFY", "1")
    monkeypatch.setenv("STOA_MODE", "api")  # no local GPU: local steps would go to Gemini unless private lifts it
    monkeypatch.setenv("STOA_TEXT_BACKEND", "gemini")
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    SEEN.clear()
    netguard.blocked(clear=True)
    RECORDING["on"] = True
    yield made
    RECORDING["on"] = RECORDING["abort"] = False


def _hosted(made: list[str]) -> list[str]:
    return [u for u in made if "203.0.113." in u]


def test_netguard_refuses_outside_addresses_in_private_mode(monkeypatch):
    monkeypatch.setenv("STOA_PRIVATE", "1")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    with pytest.raises(netguard.PrivateNetworkBlocked):
        s.connect(("203.0.113.7", 443))
    s.close()
    with pytest.raises(netguard.PrivateNetworkBlocked):
        socket.getaddrinfo("generativelanguage.googleapis.com", 443)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname(), timeout=1)  # our own machine stays reachable
    c.close()
    srv.close()
    assert any("203.0.113.7" in b for b in netguard.blocked())


def test_netguard_is_off_without_private_mode(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    netguard._hook("socket.getaddrinfo", ("generativelanguage.googleapis.com", 443, 0, 0, 0))  # no raise


def test_g1_request_flag_closes_the_network_too(monkeypatch):
    """Pipeline(private=1) on the API sets G1's context variable, not the env: netguard follows it."""
    import contextvars
    import types

    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    fake = types.ModuleType("stoa.agent.private")
    fake._CTX = contextvars.ContextVar("t2_test_private", default=False)
    monkeypatch.setitem(sys.modules, "stoa.agent.private", sys.modules.get("stoa.agent.private") or fake)
    ctx = sys.modules["stoa.agent.private"]._CTX
    assert not netguard.private_mode()
    tok = ctx.set(True)
    try:
        assert netguard.private_mode()
        with pytest.raises(netguard.PrivateNetworkBlocked):
            socket.getaddrinfo("integrate.api.nvidia.com", 443)
    finally:
        ctx.reset(tok)
    assert not netguard.private_mode()


def test_eu_only_keeps_the_verda_host(monkeypatch):
    monkeypatch.setenv("STOA_PRIVATE", "1")
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    monkeypatch.setenv("VERDA_INFERENCE_BASE_URL", "https://inference.eu.example/v1")
    assert netguard._allowed_host("inference.eu.example")
    assert not netguard._allowed_host("generativelanguage.googleapis.com")
    monkeypatch.delenv("STOA_EU_ONLY")
    assert not netguard._allowed_host("inference.eu.example")  # private without EU: Verda is outside too


def test_private_llm_call_to_a_hosted_backend_sends_nothing(hosted_stubs, monkeypatch):
    from stoa import llm

    monkeypatch.setenv("STOA_PRIVATE", "1")
    with pytest.raises(Exception):  # noqa: B017 - ExternalCallBlocked (P1) or a connection error (netguard)
        llm.chat("hello", backend="gemini", max_tokens=5, retries=0)  # chat() sleeps between retries
    assert SEEN == []


def _ask_upload():
    spec = importlib.util.spec_from_file_location("t2_ask_upload", ROOT / "web" / "server" / "ask_upload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sheet(tmp_path: Path) -> Path:
    """A small vector sheet no reader is sure about (no scale, no schedule): the cascade would escalate."""
    import fitz

    up = tmp_path / "stoa-uploads" / "t2private01"
    up.mkdir(parents=True)
    p = up / "sheet.pdf"
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    page.draw_rect(fitz.Rect(100, 100, 500, 400), width=2)
    page.draw_rect(fitz.Rect(500, 100, 700, 400), width=2)
    page.insert_text((250, 250), "STORE", fontsize=12)
    page.insert_text((570, 250), "PLANT", fontsize=12)
    doc.save(p)
    doc.close()
    return p


QUESTIONS = ["How many fire extinguishers are shown on this sheet?", "What is the scale of this drawing?"]


@pytest.mark.parametrize("question", QUESTIONS)
def test_private_upload_ask_makes_zero_outbound_calls(hosted_stubs, tmp_path, question, monkeypatch):
    mod = _ask_upload()
    monkeypatch.chdir(ROOT)
    p = _sheet(tmp_path)
    req = {"abs": str(p), "virtual": "upload/t2private01/sheet.pdf", "question": question, "private": True,
           "solver_args": {"gemini": 1, "verify": 1}}
    out = mod.answer(req)
    assert SEEN == [], f"outside connections attempted past netguard: {SEEN}"
    assert _hosted(hosted_stubs) == [], f"hosted clients built in private mode: {hosted_stubs}"
    assert netguard.blocked() == [], f"a hosted step tried and was stopped only by netguard: {netguard.blocked()}"
    assert (out.get("cost") or {}).get("api_calls", 0) == 0
    assert any("private mode" in str(t) for t in out.get("trace") or [])
    import os

    assert os.environ.get("STOA_MODE") == "api" and os.environ.get("STOA_PRIVATE") is None  # restored after


def test_control_the_same_request_not_private_would_go_out(hosted_stubs, tmp_path, monkeypatch):
    """Sensitivity check for the test above: without private mode the cascade does reach for a hosted model (and
    the recorder refuses it), so a leak in private mode would have been seen."""
    mod = _ask_upload()
    monkeypatch.chdir(ROOT)
    p = _sheet(tmp_path)
    req = {"abs": str(p), "virtual": "upload/t2private01/sheet.pdf", "question": QUESTIONS[0],
           "solver_args": {"gemini": 1, "verify": 1}}
    RECORDING["abort"] = True
    try:
        mod.answer(req)
    except (Exception, _FirstLeak):  # noqa: BLE001 - what matters is that a hosted step was attempted
        pass
    assert SEEN or _hosted(hosted_stubs), "control: no hosted step was attempted, the private test proves nothing"
