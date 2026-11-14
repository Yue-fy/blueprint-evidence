"""Private mode at the network layer: while STOA_PRIVATE=1, no socket in this process may connect outside our own
machines (audit 19:51 fix #3, captain 18:53 'drawings never leave our server').

The flags (Pipeline(private=1), ask_upload.privacy(), stoa.llm.check_private) switch hosted steps off where they
start. This module is the backstop under all of them: a PEP 578 audit hook on `socket.connect` and
`socket.getaddrinfo` that refuses any address that is not loopback, RFC 1918 / link-local, or Tailscale (100.64/10)
- whichever library opens the socket (openai/httpx, urllib, requests, a Google SDK, a future reader). With
STOA_EU_ONLY=1 Verda's hosts (W5's 'EU' option, see _eu_hosts) stay allowed. A refused connection raises
PrivateNetworkBlocked (a ConnectionRefusedError: HTTP clients report 'connection failed', readers fall back).

Installed once per process by `install()` (stoa/__init__.py calls it; STOA_NETGUARD=0 skips it). `blocked()` lists
what was refused. Cycle 2, measured destinations: with private mode off too, every outside host this process looks up
or connects to is noted (`outside()`), and `with watch() as hosts:` collects the ones of one request (threads started
with contextvars.copy_context() report into the same list), so an answer can say where its data actually went from
what the network layer saw, not only from which steps say they ran.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import os
import socket
import sys
import threading
from urllib.parse import urlparse

_installed = False
_lock = threading.Lock()
_blocked: list[str] = []
_seen: list[str] = []  # outside hosts looked up / connected to by this process (private off, or allowed by EU)
_WATCH: contextvars.ContextVar[list | None] = contextvars.ContextVar("stoa_netguard_watch", default=None)
# ours = this machine, RFC 1918 / ULA private networks, link-local, Tailscale (CGNAT 100.64/10). Not
# ipaddress.is_private: it also counts the documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) and other
# special-purpose blocks as 'private'.
_OURS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                           "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/32", "::1/128", "fc00::/7",
                                           "fe80::/10", "::/128")]
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", ""}


class PrivateNetworkBlocked(ConnectionRefusedError):
    """A connection outside our machines was refused because private mode is on (STOA_PRIVATE=1)."""


def private_mode() -> bool:
    """STOA_PRIVATE=1 (W5's upload path, the warm command), or G1's per-request flag Pipeline(private=1) = the context
    variable stoa.agent.private._CTX (read only when that module is already loaded: nothing is imported in the hook).
    A thread started without a copy of the request's context does not see the context variable; the env flag covers
    the upload worker, which answers one question at a time."""
    if os.environ.get("STOA_PRIVATE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    ctx = getattr(sys.modules.get("stoa.agent.private"), "_CTX", None)
    try:
        return bool(ctx.get()) if ctx is not None else False
    except Exception:  # noqa: BLE001
        return False


def eu_only() -> bool:
    return os.environ.get("STOA_EU_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def _local_ip(host: str) -> bool | None:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None  # a name, not an address
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in _OURS)


def _eu_hosts() -> set[str]:
    """W5's 'EU' option = our machines + Verda (EU): stoa.llm's verda backend ($VERDA_INFERENCE_BASE_URL, default
    inference.verda.com) and a local-model URL that is itself a Verda container (V1: $LLM_BASE_URL on Verda)."""
    hosts = set()
    urls = [os.environ.get("VERDA_INFERENCE_BASE_URL", "https://inference.verda.com/v1"), os.environ.get("VERDA_BASE_URL", "")]
    urls += [u for u in (os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM2_BASE_URL", "")) if "verda" in u.lower()]
    for u in urls:
        h = (urlparse(u).hostname or "").lower()
        if h:
            hosts.add(h)
    return hosts


_eu_ips: dict[str, set[str]] = {}
_resolving = threading.local()


def _eu_ip_set() -> set[str]:
    """IPs of the EU host(s), resolved on first need (the lookup's own audit event passes: the name is allowed)."""
    for h in _eu_hosts():
        if h not in _eu_ips and not getattr(_resolving, "on", False):
            _resolving.on = True
            try:
                _eu_ips[h] = {ai[4][0] for ai in socket.getaddrinfo(h, 443)}
            except OSError:
                _eu_ips[h] = set()
            finally:
                _resolving.on = False
    return {ip for h, ips in _eu_ips.items() if h in _eu_hosts() for ip in ips}


def _allowed_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in _LOCAL_NAMES:
        return True
    local = _local_ip(host)
    if local is not None:
        if local:
            return True
        return eu_only() and host in _eu_ip_set()
    return eu_only() and host in _eu_hosts()


def _refuse(what: str) -> None:
    with _lock:
        if len(_blocked) < 200:
            _blocked.append(what)
    raise PrivateNetworkBlocked(f"private mode: connection to {what} refused (STOA_PRIVATE=1; nothing was sent)")


def _note(what: str) -> None:
    lst = _WATCH.get()
    if lst is not None and what not in lst and len(lst) < 50:
        lst.append(what)
    with _lock:
        if what not in _seen and len(_seen) < 500:
            _seen.append(what)


def _hook(event: str, args: tuple) -> None:
    if event not in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"):
        return
    if not private_mode():
        try:  # observation only: must never break a connection
            _check(event, args, False)
        except Exception:  # noqa: BLE001
            pass
        return
    _check(event, args, True)


def _check(event: str, args: tuple, private: bool) -> None:
    if event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        host = str(host or "").strip("[]").lower()
        if host in _LOCAL_NAMES or _local_ip(host):
            return
        if private and not _allowed_host(host):
            _refuse(f"{host} (name lookup)")
        _note(host)
        return
    sock, addr = args[0], args[1] if len(args) > 1 else None
    fam = getattr(sock, "family", None)
    if fam not in (socket.AF_INET, socket.AF_INET6) or not isinstance(addr, tuple) or not addr:
        return  # AF_UNIX / netlink / sendto without an address: never leaves the machine on its own
    host = str(addr[0]).strip("[]").lower()
    local = _local_ip(host)
    if local or (local is None and host in _LOCAL_NAMES):
        return  # ours (a name given to connect() itself is judged like a lookup)
    where = f"{host}:{addr[1] if len(addr) > 1 else '?'}"
    if private and not _allowed_host(host):
        _refuse(where)
    _note(where)


def install() -> bool:
    """Add the audit hook to this process once. Returns True when it is (or already was) installed."""
    global _installed
    if _installed:
        return True
    if os.environ.get("STOA_NETGUARD", "1").strip() == "0":
        return False
    with _lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True
    return True


def blocked(clear: bool = False) -> list[str]:
    """What private mode refused in this process since the last clear (addresses / names)."""
    with _lock:
        out = list(_blocked)
        if clear:
            _blocked.clear()
    return out


def outside(clear: bool = False) -> list[str]:
    """Outside hosts (names from lookups, address:port from connects) this process reached since the last clear."""
    with _lock:
        out = list(_seen)
        if clear:
            _seen.clear()
    return out


@contextlib.contextmanager
def watch():
    """`with watch() as hosts:` -> the outside hosts reached inside the block (this context and threads started with
    a copy of it), e.g. around one answer: ['generativelanguage.googleapis.com', '142.250.74.10:443']."""
    hosts: list[str] = []
    tok = _WATCH.set(hosts)
    try:
        yield hosts
    finally:
        _WATCH.reset(tok)
