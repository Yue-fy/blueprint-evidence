"""Tiny Verda Cloud API client (stdlib only). Reads VERDA_CLIENT_ID / VERDA_CLIENT_SECRET from the
environment and never prints them.

  python deploy/verda/verda.py balance | types | instances | images | sshkeys | locations
  python deploy/verda/verda.py avail <instance_type>
  python deploy/verda/verda.py create <instance_type> <hostname> [--image IMG] [--spot] [--location FIN-01]
  python deploy/verda/verda.py delete <instance_id>
  python deploy/verda/verda.py ip <instance_id>

Money rules (V1.md): one instance at a time, hostname junction-*, delete as soon as a job ends.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://api.verda.com/v1"


def _req(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        raise SystemExit(f"HTTP {e.code} {method} {path}: {raw[:500]}")
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return raw


def token() -> str:
    cid, sec = os.environ.get("VERDA_CLIENT_ID"), os.environ.get("VERDA_CLIENT_SECRET")
    if not cid or not sec:
        raise SystemExit("VERDA_CLIENT_ID / VERDA_CLIENT_SECRET not set")
    r = _req("POST", "/oauth2/token", {"grant_type": "client_credentials", "client_id": cid,
                                        "client_secret": sec})
    return r["access_token"]


def get(path: str, tok: str | None = None):
    return _req("GET", path, token=tok or token())


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    tok = token()
    if cmd == "balance":
        print(json.dumps(get("/balance", tok)))
    elif cmd == "types":
        for t in get("/instance-types", tok):
            print(json.dumps({k: t.get(k) for k in ("instance_type", "price_per_hour", "spot_price",
                                                     "description", "gpu", "gpu_memory", "memory",
                                                     "cpu", "storage")}))
    elif cmd == "instances":
        for i in get("/instances", tok):
            print(json.dumps({k: i.get(k) for k in ("id", "hostname", "status", "instance_type", "ip",
                                                     "price_per_hour", "is_spot", "created_at",
                                                     "location")}))
    elif cmd in ("images", "sshkeys", "locations"):
        print(json.dumps(get("/" + {"sshkeys": "sshkeys"}.get(cmd, cmd), tok), indent=1)[:6000])
    elif cmd == "avail":
        print(json.dumps(get(f"/instance-availability/{rest[0]}", tok)))
    elif cmd == "ip":
        i = get(f"/instances/{rest[0]}", tok)
        print(i.get("ip"), i.get("status"))
    elif cmd == "create":
        itype, host = rest[0], rest[1]
        if not host.startswith("junction-"):
            raise SystemExit("hostname must start with junction-")
        live = [i for i in get("/instances", tok)
                if i.get("status") not in ("deleting", "discontinued", "offline")]
        if live:
            raise SystemExit(f"refusing: {len(live)} instance(s) already exist: "
                             + ", ".join(f"{i['hostname']}={i['status']}" for i in live))
        image = rest[rest.index("--image") + 1] if "--image" in rest else "ubuntu-24.04-cuda-12.8-open-docker"
        loc = rest[rest.index("--location") + 1] if "--location" in rest else "FIN-01"
        keys = [k["id"] for k in get("/sshkeys", tok) if k.get("name") == "junction-server"]
        body = {"instance_type": itype, "image": image, "hostname": host, "description": host,
                "ssh_key_ids": keys, "location_code": loc, "is_spot": "--spot" in rest}
        if "--disk" in rest:
            body["os_volume"] = {"name": host + "-os", "size": int(rest[rest.index("--disk") + 1])}
        print(json.dumps(_req("POST", "/instances", body, tok)))
    elif cmd == "delete":
        print(json.dumps(_req("PUT", "/instances", {"action": "delete", "id": rest[0]}, tok)))
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
