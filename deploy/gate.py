"""Shared-password gate in front of the demo (P1): a small reverse proxy, standard library only.

Stoa tests through our web UI from outside (captain, 14 Nov 12:44), so the UI gets a public link (Tailscale Funnel)
and this gate in front of it. One shared password; a login page sets a cookie (browsers), or HTTP Basic auth with any
user name (curl / scripts). Everything else is passed through unchanged and streamed both ways (uploads, PDFs, IFC
models, slow answers up to 15 min). It never touches the upstream process.

    STOA_GATE_PASSWORD='...' python deploy/gate.py --upstream http://127.0.0.1:5180 --port 8190
    # or: --password-file ~/.stoa_gate (one line, chmod 600). Never pass the password on the command line.

Open without a password: GET /__gate/health (gate + upstream status, for the watchdog and Funnel checks).
Login: /__gate/login (form). Logout: /__gate/logout. Failed logins are rate-limited (--max-fails per 10 min per client).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COOKIE = "stoa_gate"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade"}
CHUNK = 64 * 1024

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Blueprints Talk: sign in</title>
<style>
 :root {{ color-scheme: light dark; --bg:#f6f5f2; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#d9d7d0; --acc:#2f5d50; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --bg:#161614; --fg:#ecebe6; --muted:#a3a29b; --card:#20201d; --line:#3a3a35; --acc:#7fb8a5; }} }}
 body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
        font:16px/1.45 system-ui, sans-serif; padding:16px; box-sizing:border-box; }}
 form {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:28px; width:100%; max-width:360px; }}
 h1 {{ font-size:20px; margin:0 0 4px; }} p {{ color:var(--muted); margin:0 0 18px; font-size:14px; }}
 input {{ width:100%; box-sizing:border-box; padding:10px 12px; font-size:16px; border:1px solid var(--line);
         border-radius:6px; background:transparent; color:var(--fg); }}
 button {{ margin-top:12px; width:100%; padding:10px; font-size:16px; border:0; border-radius:6px; background:var(--acc);
          color:var(--card); cursor:pointer; }}
 .err {{ color:#b3261e; font-size:14px; margin-top:10px; min-height:1em; }}
</style></head><body>
<form method="post" action="/__gate/login">
 <h1>Blueprints Talk</h1><p>Team Hannah Family · Junction 2026. Enter the shared password.</p>
 <input type="hidden" name="next" value="{next}">
 <input type="password" name="password" placeholder="Password" autofocus required autocomplete="current-password">
 <button type="submit">Sign in</button><div class="err">{err}</div>
</form></body></html>"""


class Gate:
    def __init__(self, upstream: str, password: str, timeout: float, max_fails: int = 20):
        u = urllib.parse.urlsplit(upstream)
        if u.scheme != "http":
            raise SystemExit("--upstream must be http://host:port (the gate runs next to the demo)")
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.password = password
        self.token = hmac.new(password.encode(), b"stoa-gate-v1", hashlib.sha256).hexdigest()
        self.timeout = timeout
        self.max_fails = max_fails
        self.fails: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def ok_password(self, pw: str) -> bool:
        return hmac.compare_digest(pw.encode(), self.password.encode())

    def limited(self, client: str) -> bool:
        now = time.time()
        with self.lock:
            xs = [t for t in self.fails.get(client, []) if now - t < 600]
            self.fails[client] = xs
            return len(xs) >= self.max_fails

    def failed(self, client: str) -> None:
        with self.lock:
            self.fails.setdefault(client, []).append(time.time())


def make_handler(g: Gate):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "stoa-gate"

        def log_message(self, fmt, *args):  # one line per request; never headers, cookies or bodies
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self._client()} {fmt % args}\n")

        def _client(self) -> str:
            xf = self.headers.get("X-Forwarded-For", "") if hasattr(self, "headers") and self.headers else ""
            return (xf.split(",")[0].strip() or self.client_address[0]) if xf else self.client_address[0]

        # ---------------------------------------------------------------- auth
        def _authed(self) -> bool:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v, g.token):
                    return True
            a = self.headers.get("Authorization") or ""
            if a.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(a[6:]).decode().partition(":")
                    return g.ok_password(pw)
                except Exception:  # noqa: BLE001
                    return False
            return False

        def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _secure(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return self.headers.get("X-Forwarded-Proto") == "https" or host not in ("localhost", "127.0.0.1", "")

        def _login_page(self, nxt: str = "/", err: str = "", code: int = 200):
            body = LOGIN_PAGE.format(next=html.escape(nxt, quote=True), err=html.escape(err)).encode()
            self._send(code, body, "text/html; charset=utf-8")

        def _gate_routes(self) -> bool:
            path, _, query = self.path.partition("?")
            if path == "/__gate/health":
                up = "down"
                try:
                    c = http.client.HTTPConnection(g.host, g.port, timeout=5)
                    c.request("GET", "/")
                    up = str(c.getresponse().status)
                    c.close()
                except Exception as e:  # noqa: BLE001
                    up = f"down ({type(e).__name__})"
                self._send(200, json.dumps({"gate": "ok", "upstream": up}).encode(), "application/json")
                return True
            if path == "/__gate/logout":
                self._send(302, b"", extra={"Location": "/__gate/login",
                                            "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
                return True
            if path == "/__gate/login":
                nxt = urllib.parse.parse_qs(query).get("next", ["/"])[0]
                if self.command != "POST":
                    self._login_page(nxt)
                    return True
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(min(n, 8192)).decode(errors="replace"))
                nxt = form.get("next", ["/"])[0]
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                client = self._client()
                if g.limited(client):
                    self._login_page(nxt, "Too many attempts. Wait 10 minutes.", 429)
                    return True
                if not g.ok_password(form.get("password", [""])[0]):
                    g.failed(client)
                    self._login_page(nxt, "Wrong password.", 401)
                    return True
                cookie = f"{COOKIE}={g.token}; Path=/; Max-Age=604800; HttpOnly; SameSite=Lax"
                if self._secure():
                    cookie += "; Secure"
                self._send(303, b"", extra={"Location": nxt, "Set-Cookie": cookie})
                return True
            return False

        # ---------------------------------------------------------------- proxy
        def _proxy(self):
            if self._gate_routes():
                return
            if not self._authed():
                if self.headers.get("Content-Length"):  # drain the body so the connection stays usable
                    self.rfile.read(int(self.headers["Content-Length"]))
                if "text/html" in (self.headers.get("Accept") or "") and self.command in ("GET", "HEAD"):
                    q = urllib.parse.quote(self.path, safe="")
                    self._send(302, b"", extra={"Location": f"/__gate/login?next={q}"})
                else:
                    self._send(401, b'{"error": "password required: open the page and sign in"}', "application/json")
                return
            headers = {}
            for k, v in self.headers.items():
                kl = k.lower()
                if kl in HOP or kl in ("host", "authorization", "content-length"):
                    continue
                if kl == "cookie":  # our cookie stays here
                    v = "; ".join(p for p in v.split(";") if p.strip() and not p.strip().startswith(COOKIE + "="))
                    if not v:
                        continue
                headers[k] = v
            headers["Host"] = f"{g.host}:{g.port}"
            headers["X-Forwarded-For"] = self._client()
            headers["X-Forwarded-Proto"] = "https" if self._secure() else "http"
            n = self.headers.get("Content-Length")
            chunked_in = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
            try:
                up = http.client.HTTPConnection(g.host, g.port, timeout=g.timeout)
                up.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in headers.items():
                    up.putheader(k, v)
                if n is not None:
                    up.putheader("Content-Length", n)
                elif chunked_in:
                    up.putheader("Transfer-Encoding", "chunked")
                up.endheaders()
                if n is not None:  # stream the request body (uploads)
                    left = int(n)
                    while left > 0:
                        b = self.rfile.read(min(CHUNK, left))
                        if not b:
                            break
                        up.send(b)
                        left -= len(b)
                elif chunked_in:
                    while True:
                        size = int(self.rfile.readline().strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            up.send(b"0\r\n\r\n")
                            break
                        data = self.rfile.read(size)
                        self.rfile.readline()
                        up.send(f"{size:x}\r\n".encode() + data + b"\r\n")
                r = up.getresponse()
            except Exception as e:  # noqa: BLE001
                self._send(502, json.dumps({"error": f"demo not reachable ({type(e).__name__})"}).encode(),
                           "application/json")
                return
            self.send_response(r.status, r.reason)
            length = r.getheader("Content-Length")
            for k, v in r.getheaders():
                if k.lower() in HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            no_body = self.command == "HEAD" or r.status in (204, 304) or 100 <= r.status < 200
            if length is not None:
                self.send_header("Content-Length", length)
            elif not no_body:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                if not no_body:
                    while True:
                        b = r.read1(CHUNK) if hasattr(r, "read1") else r.read(CHUNK)
                        if not b:
                            break
                        self.wfile.write(b if length is not None else f"{len(b):x}\r\n".encode() + b + b"\r\n")
                    if length is None:
                        self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            finally:
                up.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _proxy

    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", default="http://127.0.0.1:5180", help="the demo web server (default :5180)")
    ap.add_argument("--host", default="127.0.0.1", help="listen address (Funnel connects locally)")
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--password-file", help="file with the password on its first line (else $STOA_GATE_PASSWORD)")
    ap.add_argument("--timeout", type=float, default=900, help="seconds to wait for the demo (slow answers)")
    ap.add_argument("--max-fails", type=int, default=20, help="failed logins per client per 10 min before a 429 (if the "
                    "tunnel hides client addresses this is shared by everyone: keep the password strong, not this low)")
    a = ap.parse_args(argv)
    pw = Path(a.password_file).expanduser().read_text().splitlines()[0].strip() if a.password_file \
        else os.environ.get("STOA_GATE_PASSWORD", "")
    if len(pw) < 8:
        print("set a password of at least 8 characters: $STOA_GATE_PASSWORD or --password-file", file=sys.stderr)
        return 2
    g = Gate(a.upstream, pw, a.timeout, a.max_fails)
    try:
        srv = ThreadingHTTPServer((a.host, a.port), make_handler(g))
    except OSError as e:  # never let a Funnel point at whatever else owns the port
        print(f"cannot listen on {a.host}:{a.port} ({e}); pick a free --port and point the Funnel at it only after "
              f"/__gate/health answers {{\"gate\": \"ok\"}}", file=sys.stderr)
        return 3
    srv.daemon_threads = True
    print(f"gate on http://{a.host}:{a.port} -> {a.upstream} (health: /__gate/health)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
