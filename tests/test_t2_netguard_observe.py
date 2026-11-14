"""T2 cycle 2: netguard notes the outside hosts a request reached (measured destinations), private or not."""

from __future__ import annotations

import contextvars
import socket
import threading

import pytest

from stoa import netguard


class _Sock:
    family = socket.AF_INET


def test_outside_hosts_are_noted_with_private_off(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    netguard.outside(clear=True)
    with netguard.watch() as hosts:
        netguard._hook("socket.getaddrinfo", ("generativelanguage.googleapis.com", 443, 0, 0, 0))
        netguard._hook("socket.connect", (_Sock(), ("203.0.113.9", 443)))
        netguard._hook("socket.getaddrinfo", ("localhost", 8001, 0, 0, 0))  # ours: not noted
        netguard._hook("socket.connect", (_Sock(), ("127.0.0.1", 8001)))
        netguard._hook("socket.connect", (_Sock(), ("10.1.2.3", 8001)))
    assert hosts == ["generativelanguage.googleapis.com", "203.0.113.9:443"]
    assert netguard.outside() == hosts
    with netguard.watch() as other:  # a new request starts empty
        pass
    assert other == []


def test_threads_with_a_copied_context_report_into_the_same_request(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    with netguard.watch() as hosts:
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(netguard._hook, "socket.getaddrinfo",
                                                   ("integrate.api.nvidia.com", 443, 0, 0, 0)))
        t.start()
        t.join()
    assert hosts == ["integrate.api.nvidia.com"]


def test_private_mode_still_refuses_names_given_to_connect(monkeypatch):
    monkeypatch.setenv("STOA_PRIVATE", "1")
    netguard.blocked(clear=True)
    with netguard.watch() as hosts:
        with pytest.raises(netguard.PrivateNetworkBlocked):
            netguard._hook("socket.connect", (_Sock(), ("generativelanguage.googleapis.com", 443)))
        with pytest.raises(netguard.PrivateNetworkBlocked):
            netguard._hook("socket.connect", (_Sock(), ("203.0.113.9", 443)))
        netguard._hook("socket.connect", (_Sock(), ("localhost", 8001)))
    assert hosts == []  # refused, so nothing was reached
    assert len(netguard.blocked()) == 2


def test_observation_never_breaks_a_connection_with_private_off(monkeypatch):
    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    for args in [(), (None,), (_Sock(),), (_Sock(), ("::ffff:1.2.3.4%eth0", 1)), (_Sock(), (None, None)), (b"\xff",)]:
        netguard._hook("socket.getaddrinfo", args)
        netguard._hook("socket.connect", args)
