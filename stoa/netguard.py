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

Cycle 3, kept-alive connections: reusing a pooled connection (stoa.llm caches one OpenAI client per backend; httpx
keeps idle connections 5 s) raises NO audit event, so neither the guard nor the measurement saw it. Every outside TCP
socket this process connects is now tracked by file descriptor + peer address: (1) when a private request starts they
are shut down (exactly at os.environ['STOA_PRIVATE'] = '1' in the upload worker; for G1's per-request context variable
at the request's first audited event, once per private episode, so a concurrent non-private request's new connection
is not killed again and again), so a private request can never send on a connection opened before it (the client sees
a closed connection, reconnects, and that connect is refused); allowed hosts (STOA_EU_ONLY=1: Verda) stay open;
(2) `watch()` reads each tracked socket's bytes-sent counter (Linux TCP_INFO) at the start and end of the block, so a
reused connection that sent data counts as a destination of that request. Cost per audit event: ~50 ns with no outside
connection open, ~1.4 µs while one is (a 13 s area answer raises ~19k events).

Cycle 6, this machine's ports that forward outside: a connect to 127.0.0.1:P is outside in effect when P is an ssh
tunnel to a machine elsewhere or a local service set to pass its work on (V1's det_server with DET_REMOTE_URL -> a GPU
twin on a rented Verda instance; live on :8145 / :8148 on Sat night). `relays()` finds them from /proc (cached 10 s,
~20-30 ms per scan; ~1.4 µs per loopback connect otherwise); such a connect is refused in private mode, noted as a
destination ('127.0.0.1:8148 (forwards outside: DET_REMOTE_URL -> ssh tunnel)') with it off, and a kept-alive one is
closed when a private request starts. `relay_for_url(url)` answers it for a service URL (the privacy note uses it).

Cycle 7, nothing goes out after the answer was returned: a hosted step can run on in a background thread after its
answer has been returned (G1's vision reading waits STOA_VISION_WAIT = 25 s, then 'the local answer stands'; the
request itself queues in stoa.llm's machine-wide Gemini throttle, 6 s per call, and can be SENT after the wait gave
up: the demo's log has a send 1.9 s after the wait ended, 00:18:55 / 00:19:02). The answer's card is already written,
so such a send went to Google without showing anywhere. A thread that carries an answer's watch() context (copied
with contextvars, like every thread an answer starts) is now refused any new outside lookup / connect once the block
has ended, private mode on or off (PrivateNetworkBlocked; nothing is sent; `late()` lists them). A request already
in flight keeps its connection (its reply, and the reply cache, still arrive). Threads not started by an answer are
not affected. STOA_NETGUARD_LATE=0 turns it off. A late request on a KEPT-ALIVE connection raises no connect event:
`answer_returned()` lets the caller (stoa.llm, before its throttle) refuse it first.
"""

from __future__ import annotations

import contextlib
import contextvars
import ipaddress
import os
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlparse

_installed = False
_lock = threading.Lock()
_blocked: list[str] = []  # connections / lookups private mode REFUSED (something tried to go out)
_closed: list[str] = []  # kept-alive outside connections shut down when a private request started (nothing tried)
_seen: list[str] = []  # outside hosts looked up / connected to by this process (private off, or allowed by EU)
_late: list[str] = []  # outside lookups / connects refused because their answer had already been returned (cycle 7)
_WATCH: contextvars.ContextVar[list | None] = contextvars.ContextVar("stoa_netguard_watch", default=None)
# outside TCP sockets this process connected: fd -> (label, peer host, peer port). Read without the lock in the hook
# (a dict's truthiness is atomic); changed under _lock.
_tracked: dict[int, tuple[str, str, int]] = {}
_relayed: set[int] = set()  # tracked fds whose peer is a port of THIS machine that forwards outside (cycle 6)
_TL = threading.local()  # .name = the last outside name this thread looked up (labels the connect that follows);
#                          .busy = inside netguard's own socket calls (no re-entry from their audit events)
# ours = this machine, RFC 1918 / ULA private networks, link-local, Tailscale (CGNAT 100.64/10). Not
# ipaddress.is_private: it also counts the documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) and other
# special-purpose blocks as 'private'.
_OURS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                           "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/32", "::1/128", "fc00::/7",
                                           "fe80::/10", "::/128")]
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", ""}


class PrivateNetworkBlocked(ConnectionRefusedError):
    """A connection outside our machines was refused because private mode is on (STOA_PRIVATE=1), or because the
    answer it would serve was already returned (cycle 7; see late())."""


class _Watch(list):
    """watch()'s list of the outside hosts one answer reached; .done once the block has ended (answer returned)."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        super().__init__()
        self.done = False


def _late_on() -> bool:
    return os.environ.get("STOA_NETGUARD_LATE", "1").strip() != "0"


def _after_answer(what: str) -> None:
    """Refuse an outside destination asked for by a thread working for an answer that was already returned."""
    lst = _WATCH.get()
    if lst is None or not getattr(lst, "done", False) or not _late_on():
        return
    with _lock:
        if len(_late) < 200:
            _late.append(what)
    raise PrivateNetworkBlocked(f"connection to {what} refused: the answer it was for has already been returned, so "
                                "it would not show where that answer's data went (nothing was sent)")


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


_SOCKET_EVENTS = frozenset(("socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"))


_TRUE = ("1", "true", "yes", "on")


def _turns_private_on(event: str, args: tuple) -> bool:
    """os.environ['STOA_PRIVATE'] = '1' (ask_upload.privacy(), the warm command): the os.putenv event comes BEFORE the
    mapping changes, so private_mode() is still False while it runs."""
    if event != "os.putenv" or len(args) < 2:
        return False
    k, v = args[0], args[1]
    k = k.decode("ascii", "replace") if isinstance(k, bytes) else str(k)
    v = v.decode("ascii", "replace") if isinstance(v, bytes) else str(v)
    return k == "STOA_PRIVATE" and v.strip().lower() in _TRUE


# set in a request's context once its private start has closed the kept-alive connections: closing again at every
# later event would also kill connections that CONCURRENT non-private requests (other API threads) open meanwhile
_CLOSED = contextvars.ContextVar("stoa_netguard_closed", default=False)


def _hook(event: str, args: tuple) -> None:
    closed = _CLOSED.get()
    if (_tracked or closed) and not getattr(_TL, "busy", False):  # cheap checks first: most events stop here
        # an outside connection is open (kept alive by a client's pool): a private request must not find it
        # usable. Exactly when STOA_PRIVATE=1 is set (the upload worker: one question at a time), else once per
        # private episode of a request context, at its first audited event (G1's Pipeline(private=1) context
        # variable); the marker clears at the first event after the episode, so the next one closes again
        try:
            if _tracked and _turns_private_on(event, args):
                _close_tracked()
            else:
                p = private_mode()
                if p and not closed and _tracked:
                    _CLOSED.set(True)
                    _close_tracked()
                elif closed and not p:
                    _CLOSED.set(False)
        except Exception:  # noqa: BLE001 (never break the event that triggered the check)
            pass
    if event not in _SOCKET_EVENTS:
        return
    if not private_mode():
        try:  # observation only: must never break a connection ...
            _check(event, args, False)
        except PrivateNetworkBlocked:  # ... except a send after its answer was returned (cycle 7: _after_answer)
            raise
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
        _after_answer(f"{host} (name lookup)")
        if private and not _allowed_host(host):
            _refuse(f"{host} (name lookup)")
        _note(host)
        if _local_ip(host) is None:
            _TL.name = host  # the connect that follows in this thread goes to this name
        return
    sock, addr = args[0], args[1] if len(args) > 1 else None
    fam = getattr(sock, "family", None)
    if fam not in (socket.AF_INET, socket.AF_INET6) or not isinstance(addr, tuple) or not addr:
        return  # AF_UNIX / netlink / sendto without an address: never leaves the machine on its own
    host = str(addr[0]).strip("[]").lower()
    local = _local_ip(host)
    if local or (local is None and host in _LOCAL_NAMES):
        # ours (a name given to connect() itself is judged like a lookup) -- unless this machine's port forwards
        # outside: an ssh tunnel, or a local service set to pass its work to an outside machine (cycle 6)
        if event == "socket.connect" and len(addr) > 1 and isinstance(addr[1], int) and _loopback(host):
            why = _relay_for(addr[1])
            if why:
                where = f"{host}:{addr[1]} ({why})"
                _after_answer(where)
                if private and not (eu_only() and addr[1] in _eu_relay_ports()):
                    _refuse(where)
                _note(where)
                if getattr(sock, "type", None) == socket.SOCK_STREAM:
                    _track(sock, host, addr[1], relay=where)
        return
    port = addr[1] if len(addr) > 1 else "?"
    where = f"{host}:{port}"
    _after_answer(where)
    if private and not _allowed_host(host):
        _refuse(where)
    _note(where)
    if event == "socket.connect" and getattr(sock, "type", None) == socket.SOCK_STREAM:
        _track(sock, host, port)


# ---- this machine's ports that forward outside (cycle 6) ----------------------------------------------------------
# 127.0.0.1 is not always 'ours': an ssh tunnel (`ssh -L 127.0.0.1:P:...` or `-D P`) carries what is sent to port P to
# a machine elsewhere, and a local service can pass its work on (V1's det_server with DET_REMOTE_URL sends each page
# image to a GPU twin on a rented Verda instance through such a tunnel). Neither shows at this process's socket: the
# connect goes to 127.0.0.1. So a loopback connect is checked against this machine's forwarding ports, from /proc
# (Linux), cached for STOA_NETGUARD_RELAY_TTL seconds (default 10; a scan of ~1,350 processes takes ~20 ms):
#  - a port listened on by an ssh / autossh process of ours that has a connection to an address outside our networks;
#  - a port listened on by a process of ours whose environment sets a *_REMOTE_URL to an outside host or to one of
#    those tunnel ports (the det_server convention; nothing else in the team uses that suffix).
# Only processes of this user can be read (another user's tunnel is not seen). With private mode on such a connect is
# refused like an outside one (STOA_EU_ONLY=1 too: a tunnel's far end cannot be told to be Verda, unless the operator
# names the port in STOA_EU_RELAY_PORTS); with it off it is noted as a destination. Off-Linux, or with
# STOA_NETGUARD_RELAYS=0, nothing is checked.

_RELAY_COMMS = frozenset(("ssh", "autossh"))
_relay_cache: dict = {"t": None, "ports": {}}
_relay_lock = threading.Lock()


def _loopback(host: str) -> bool:
    if host in _LOCAL_NAMES:
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def _hexaddr(s: str) -> tuple[str, int]:
    """'0100007F:1F90' (/proc/net/tcp) -> ('127.0.0.1', 8080); tcp6 holds four little-endian 32-bit words."""
    ip_hex, port_hex = s.split(":")
    b = bytes.fromhex(ip_hex)
    if len(b) == 4:
        return socket.inet_ntop(socket.AF_INET, b[::-1]), int(port_hex, 16)
    b = b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4))
    ip = ipaddress.ip_address(b)
    return str(ip.ipv4_mapped or ip), int(port_hex, 16)


def _tcp_tables(root: str = "/proc") -> tuple[dict[int, int], dict[int, tuple[str, int]]]:
    """inode -> port of each socket listening where a loopback connect reaches (127/8, ::1, 0.0.0.0, ::), and
    inode -> peer of each established connection."""
    listen: dict[int, int] = {}
    est: dict[int, tuple[str, int]] = {}
    for fn in ("net/tcp", "net/tcp6"):
        try:
            with open(f"{root}/{fn}") as fh:
                lines = fh.read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 10 or not f[9].isdigit() or f[9] == "0":
                continue
            try:
                if f[3] == "0A":
                    ip, port = _hexaddr(f[1])
                    if _loopback(ip):
                        listen[int(f[9])] = port
                elif f[3] == "01":
                    est[int(f[9])] = _hexaddr(f[2])
            except ValueError:
                continue
    return listen, est


def _socket_inodes(root: str, pid: str) -> set[int]:
    out: set[int] = set()
    try:
        fds = os.listdir(f"{root}/{pid}/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            link = os.readlink(f"{root}/{pid}/fd/{fd}")
        except OSError:
            continue
        if link.startswith("socket:[") and link[8:-1].isdigit():
            out.add(int(link[8:-1]))
    return out


def _outside_url(url: str, tunnels: dict[int, str]) -> str | None:
    try:
        u = urlparse(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return None
    if not host:
        return None
    if _loopback(host):
        return tunnels.get(port or (443 if u.scheme == "https" else 80))
    loc = _local_ip(host)
    return None if loc else host  # a name or an outside address


def _scan_relays(root: str = "/proc") -> dict[int, str]:
    """port -> why it forwards outside, for this machine's loopback ports (see the section comment)."""
    listen, est = _tcp_tables(root)
    if not listen:
        return {}
    ssh, remote = [], []
    try:
        pids = [p for p in os.listdir(root) if p.isdigit()]
    except OSError:
        return {}
    for p in pids:
        try:
            with open(f"{root}/{p}/comm") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if comm in _RELAY_COMMS:
            ssh.append(p)
            continue
        try:
            with open(f"{root}/{p}/environ", "rb") as fh:
                env = fh.read()
        except OSError:  # another user's process
            continue
        if b"_REMOTE_URL=" not in env:
            continue
        urls = []
        for kv in env.split(b"\0"):
            k, _, v = kv.partition(b"=")
            if k.endswith(b"_REMOTE_URL") and v.strip():
                urls.append((k.decode("ascii", "replace"), v.decode("utf-8", "replace").strip()))
        if urls:
            remote.append((p, urls))
    tunnels: dict[int, str] = {}
    for p in ssh:
        inos = _socket_inodes(root, p)
        if any(i in est and not _local_ip(est[i][0]) for i in inos):
            for i in inos:
                if i in listen:
                    tunnels[listen[i]] = "forwards outside: ssh tunnel"
    out = dict(tunnels)
    for p, urls in remote:
        why = None
        for k, v in urls:
            far = _outside_url(v, tunnels)
            if far:
                why = f"forwards outside: {k} -> " + ("ssh tunnel" if far in tunnels.values() else far)
                break
        if why:
            for i in _socket_inodes(root, p):
                if i in listen:
                    out.setdefault(listen[i], why)
    return out


def relays(refresh: bool = False) -> dict[int, str]:
    """This machine's loopback ports that forward outside -> why (cached; see the section comment)."""
    if os.environ.get("STOA_NETGUARD_RELAYS", "1").strip() == "0" or not sys.platform.startswith("linux"):
        return {}
    try:
        ttl = float(os.environ.get("STOA_NETGUARD_RELAY_TTL", "10"))
    except ValueError:
        ttl = 10.0
    now = time.monotonic()
    t = _relay_cache["t"]
    if not refresh and t is not None and now - t < ttl:
        return _relay_cache["ports"]
    if not _relay_lock.acquire(blocking=t is None or refresh):
        return _relay_cache["ports"]  # another thread is scanning: use the last map meanwhile
    busy = getattr(_TL, "busy", False)
    _TL.busy = True  # the scan's own file reads raise audit events
    try:
        try:
            ports = _scan_relays()
        except Exception:  # noqa: BLE001 - a scan that fails keeps the last map
            ports = _relay_cache["ports"]
        _relay_cache.update(t=time.monotonic(), ports=ports)
        return ports
    finally:
        _TL.busy = busy
        _relay_lock.release()


def _eu_relay_ports() -> set[int]:
    """STOA_EU_RELAY_PORTS='8145,8148': forwarding ports the operator states end at our own EU instance (Verda,
    Finland). Allowed in private mode ONLY with STOA_EU_ONLY=1 (W5's 'EU' option = our machines + Verda), like
    Verda's hosted models; strict private mode refuses them too."""
    out = set()
    for p in os.environ.get("STOA_EU_RELAY_PORTS", "").replace(";", ",").split(","):
        if p.strip().isdigit():
            out.add(int(p.strip()))
    return out


def _relay_for(port: int) -> str | None:
    if getattr(_TL, "busy", False):
        return None
    return relays().get(port)


def relay_for_url(url: str) -> str | None:
    """Why `url` (a service URL on this machine, e.g. $STOA_DET_URL) forwards outside, else None.
    Cycle 7 (H 01:30): in private mode a forwarding service is about to be refused (stoa.llm.check_private_url asks
    this first and does not call it), so it is recorded in blocked() like a refused socket: the upload worker's
    'private mode: N outside connection(s) refused (...)' line then says which reader private mode kept off."""
    try:
        u = urlparse(url)
        host, port = (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    why = relays().get(port) if host and _loopback(host) else None
    if why and private_mode() and not (eu_only() and port in _eu_relay_ports()):
        what = f"{host}:{port} ({why}; not called)"
        with _lock:
            if what not in _blocked and len(_blocked) < 200:
                _blocked.append(what)
    return why


# ---- kept-alive outside connections (cycle 3) ---------------------------------------------------------------------

def _track(sock, host: str, port, relay: str | None = None) -> None:
    try:
        fd = sock.fileno()
    except Exception:  # noqa: BLE001
        return
    if not isinstance(fd, int) or fd < 0 or not isinstance(port, int):
        return
    name = getattr(_TL, "name", None)
    _TL.name = None
    label = relay or (f"{name} ({host}:{port})" if name and name != host else f"{host}:{port}")
    with _lock:
        _tracked[fd] = (label, host, port)
        if relay:
            _relayed.add(fd)
        else:
            _relayed.discard(fd)
        if len(_tracked) > 64:  # keep the table small: drop entries whose socket is gone
            _prune_locked()


def _open_dup(fd: int, host: str, port: int):
    """A duplicate of fd when it is still the tracked connection (same peer), else None. The caller closes it; closing
    a duplicate leaves the original open."""
    _TL.busy = True
    s = None
    try:
        s = socket.socket(fileno=os.dup(fd))
        peer = s.getpeername()
        if s.type != socket.SOCK_STREAM or str(peer[0]).lower() != host or peer[1] != port:
            s.close()
            return None
        return s
    except OSError:
        if s is not None:
            s.close()
        return None
    finally:
        _TL.busy = False


def _prune_locked() -> None:
    for fd, (_, host, port) in list(_tracked.items()):
        s = _open_dup(fd, host, port)
        if s is None:
            _tracked.pop(fd, None)
            _relayed.discard(fd)
        else:
            s.close()


def _close_tracked() -> None:
    """Shut down every tracked outside connection (private mode starts while one is open). shutdown() acts on the
    connection itself, so the client's own socket object sees it closed and must reconnect (and that connect is
    refused)."""
    with _lock:
        snap = list(_tracked.items())
        relayed = set(_relayed)
    # allowed hosts stay (STOA_EU_ONLY=1: Verda; a private request may be using that connection right now). Checked
    # outside the lock and with re-entry off: resolving the EU host raises an audit event of its own. A connection to
    # one of this machine's ports that forwards outside (cycle 6) is closed like an outside one
    _TL.busy = True
    try:
        eu_relays = _eu_relay_ports() if eu_only() else set()
        items = [(fd, v) for fd, v in snap
                 if (v[2] not in eu_relays if fd in relayed else not _allowed_host(v[1]))]
    finally:
        _TL.busy = False
    with _lock:
        for fd, v in items:
            if _tracked.get(fd) == v:
                _tracked.pop(fd, None)
                _relayed.discard(fd)
    for fd, (label, host, port) in items:
        s = _open_dup(fd, host, port)
        if s is None:
            continue
        _TL.busy = True
        try:
            s.shutdown(socket.SHUT_RDWR)
            with _lock:
                if len(_closed) < 200:
                    _closed.append(f"{label} (kept-alive connection closed: private mode)")
        except OSError:
            pass
        finally:
            s.close()
            _TL.busy = False


def _bytes_sent(s) -> int | None:
    """Bytes this TCP connection has sent so far (Linux TCP_INFO: tcpi_bytes_sent, else tcpi_bytes_acked)."""
    opt = getattr(socket, "TCP_INFO", None)
    if opt is None:
        return None
    try:
        b = s.getsockopt(socket.IPPROTO_TCP, opt, 256)
    except OSError:
        return None
    if len(b) >= 208:
        return struct.unpack_from("<Q", b, 200)[0]
    if len(b) >= 128:
        return struct.unpack_from("<Q", b, 120)[0]
    return None


def _sent_by_tracked() -> dict[int, tuple[str, int]]:
    """fd -> (label, bytes sent) for the tracked connections that are still open; closed ones are forgotten."""
    out: dict[int, tuple[str, int]] = {}
    with _lock:
        items = list(_tracked.items())
    gone = []
    for fd, (label, host, port) in items:
        s = _open_dup(fd, host, port)
        if s is None:
            gone.append(fd)
            continue
        try:
            n = _bytes_sent(s)
        finally:
            s.close()
        if n is not None:
            out[fd] = (label, n)
    if gone:
        with _lock:
            for fd in gone:
                if _tracked.get(fd, (None,))[0] == dict(items).get(fd, (None,))[0]:
                    _tracked.pop(fd, None)
                    _relayed.discard(fd)
    return out


def _forget_all() -> None:
    """Tests: stop tracking every connection (does not close them)."""
    with _lock:
        _tracked.clear()
        _relayed.clear()


# ---- public ------------------------------------------------------------------------------------------------------

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
    """What private mode refused in this process since the last clear (addresses / names): something tried to go out."""
    with _lock:
        out = list(_blocked)
        if clear:
            _blocked.clear()
    return out


def closed(clear: bool = False) -> list[str]:
    """Kept-alive outside connections (left open by earlier non-private requests) shut down when private mode started,
    since the last clear. Not a refusal: nothing in the private request tried to use them."""
    with _lock:
        out = list(_closed)
        if clear:
            _closed.clear()
    return out


def outside(clear: bool = False) -> list[str]:
    """Outside hosts (names from lookups, address:port from connects) this process reached since the last clear."""
    with _lock:
        out = list(_seen)
        if clear:
            _seen.clear()
    return out


def late(clear: bool = False) -> list[str]:
    """Outside lookups / connects refused since the last clear because the answer they were for had already been
    returned (cycle 7): work that ran on after its answer, e.g. a hosted reading sent after its wait gave up."""
    with _lock:
        out = list(_late)
        if clear:
            _late.clear()
    return out


def answer_returned() -> bool:
    """True in a thread working for an answer whose watch() block has ended: a hosted request started now would not
    show on that answer. For callers that may reuse a kept-alive connection (no connect event): check before sending."""
    lst = _WATCH.get()
    return bool(lst is not None and getattr(lst, "done", False) and _late_on())


# ---- what an answer says about the network (cycle 7: shared by stoa.api; web/server/ask_upload.py has its own copy) --
_PROVIDERS = (("relay", ("forwards outside",)), ("gemini", ("googleapis.com", "generativelanguage")),
              ("nvidia", ("nvidia.com",)), ("verda", ("verda",)))


def providers(seen: list[str]) -> list[str]:
    """watch()'s hosts -> data_used names: '...googleapis.com' -> gemini, '...nvidia.com' -> nvidia, '...verda...' ->
    verda, a forwarding port of this machine -> relay, any other outside host -> hosted. A bare 'address:port' is the
    connect that follows a named lookup, so it counts (as 'hosted') only when nothing was named."""
    import re

    out: list[str] = []
    bare = False
    for h in seen:
        hl = str(h).lower()
        name = next((p for p, keys in _PROVIDERS if any(k in hl for k in keys)), None)
        if name is None and re.match(r"^\[?[0-9a-f.:]+\]?:\d+$", hl.strip()):
            bare = True
            continue
        name = name or "hosted"
        if name not in out:
            out.append(name)
    if bare and not any(p != "relay" for p in out):
        out.append("hosted")
    return out


_SERVICE_NAMES = (("DET_REMOTE_URL", "the door and window finder"), ("OCR", "the text reader"))


def kept_off(refused: list[str]) -> list[str]:
    """blocked() items for services private mode kept off because they forward outside -> plain sentences for the
    answer's 'missing' list (H 01:30: a door count fell back to a guess and never said why)."""
    out: list[str] = []
    for r in map(str, refused or []):
        if "forwards outside" not in r:
            continue
        name = next((n for k, n in _SERVICE_NAMES if k in r), "one of our readers")
        where = r.split(" (", 1)[0]
        line = (f"Private mode kept {name} off: on this server it passes page images to a machine outside ({where}). "
                "Turn Private off to use it.")
        if line not in out:
            out.append(line)
    return out


def tell(ans, seen: list[str]) -> None:
    """Make an Answer say what the network layer saw during it (the card's 'where your data went' reads these):
    a 'network: this answer reached <hosts>' trace line, every provider seen added to data_used (trace line + field),
    cost.api_calls raised to the number of hosted providers seen. Nothing changes when nothing outside was reached."""
    import re

    seen = sorted(dict.fromkeys(map(str, seen or [])))
    if not seen:
        return
    trace = [str(t) for t in (ans.trace or [])]
    trace.append("network: this answer reached " + ", ".join(seen))
    provs = providers(seen)
    i = next((j for j, t in enumerate(trace) if t.startswith("data_used:")), None)
    rest = trace[i].split(":", 1)[1].strip() if i is not None else "local"
    if rest == "local":
        trace.append(f"data_used says local but the network saw {', '.join(seen)}")
    have = {t for t in re.split(r"[\s,;+()]+", rest.lower()) if t}
    add = [p for p in provs if p not in have]
    if add:
        line = f"data_used: {rest}, {', '.join(add)}"
        trace = [*trace[:i], line, *trace[i + 1:]] if i is not None else [*trace, line]
    ans.trace = trace
    if isinstance(getattr(ans, "data_used", None), list):
        ans.data_used = [*ans.data_used, *[p for p in provs if p not in ans.data_used]]
    calls = len([p for p in provs if p != "relay"])  # a forwarding detector is not a hosted API call
    cost = getattr(ans, "cost", None)
    if calls and cost is not None and (cost.api_calls or 0) < calls:
        cost.api_calls = calls


@contextlib.contextmanager
def watch():
    """`with watch() as hosts:` -> the outside hosts reached inside the block: new lookups / connects in this context
    and in threads started with a copy of it, e.g. ['generativelanguage.googleapis.com', '142.250.74.10:443'], plus
    every kept-alive outside connection of this PROCESS that sent bytes during the block (labelled
    'name (address:port)'; a connection shared with a concurrent request is counted for both).
    Cycle 7: when the block ends, threads still carrying its context may not open new outside connections (late())."""
    hosts = _Watch()
    before = _sent_by_tracked() if _tracked else {}
    tok = _WATCH.set(hosts)
    try:
        yield hosts
    finally:
        hosts.done = True  # first: from here on a send would not show on this answer (_after_answer)
        _WATCH.reset(tok)
        if before:
            for fd, (label, n) in _sent_by_tracked().items():
                was = before.get(fd)
                if was is not None and was[0] == label and n > was[1]:
                    host = label.split(" (", 1)[-1].rstrip(")")
                    if not any(host in h or h in label for h in hosts) and len(hosts) < 50:
                        hosts.append(label)
