"""Minimal S3 client for Verda Object Storage (stdlib only, AWS SigV4, path-style).

Keys come from ~/.config/junction-verda/s3.json ({"access_key","secret_key"}, mode 0600, never printed or committed);
`serverless.py storage-init` creates it through the Verda Cloud API.

  python deploy/verda/s3.py put <bucket> <key> <local_file>
  python deploy/verda/s3.py presign <bucket> <key> [expires_s=604800]   # prints a presigned GET URL (treat as secret)
  python deploy/verda/s3.py ls <bucket>
  python deploy/verda/s3.py rm <bucket> <key>
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = os.environ.get("VERDA_S3_ENDPOINT", "https://objects.eu-north-3.verda.storage")  # FIN-03
REGION = os.environ.get("VERDA_S3_REGION", "eu-north-3")
KEYFILE = Path(os.environ.get("VERDA_S3_KEYFILE", Path.home() / ".config/junction-verda/s3.json"))


def _keys() -> tuple[str, str]:
    k = json.loads(KEYFILE.read_text())
    return k["access_key"], k["secret_key"]


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, date: str) -> bytes:
    k = _sign(("AWS4" + secret).encode(), date)
    for part in (REGION, "s3", "aws4_request"):
        k = _sign(k, part)
    return k


def _uri(bucket: str, key: str = "") -> str:
    return "/" + bucket + ("/" + urllib.parse.quote(key, safe="/~") if key else "")


def _signed_request(method: str, bucket: str, key: str = "", query: dict | None = None, body=None,
                    payload_hash: str = hashlib.sha256(b"").hexdigest(), extra: dict | None = None):
    ak, sk = _keys()
    host = urllib.parse.urlparse(ENDPOINT).netloc
    now = dt.datetime.now(dt.timezone.utc)
    amz, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    q = "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(str(v), safe='~')}"
                 for k, v in sorted((query or {}).items()))
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz, **(extra or {})}
    signed = ";".join(sorted(headers))
    canon = "\n".join([method, _uri(bucket, key), q,
                       "".join(f"{h}:{headers[h]}\n" for h in sorted(headers)), signed, payload_hash])
    scope = f"{date}/{REGION}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date), sts.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
    req = urllib.request.Request(ENDPOINT + _uri(bucket, key) + ("?" + q if q else ""), data=body, method=method)
    for h, v in headers.items():
        if h != "host":
            req.add_header(h, v)
    with urllib.request.urlopen(req, timeout=1800) as r:
        return r.status, r.read()


def put(bucket: str, key: str, path: str) -> None:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    with p.open("rb") as f:
        st, _ = _signed_request("PUT", bucket, key, body=f, payload_hash=h.hexdigest(),
                                extra={"content-length": str(p.stat().st_size)})
    print("PUT", st, bucket, key, p.stat().st_size, "bytes")


def presign(bucket: str, key: str, expires: int = 604800) -> str:
    ak, sk = _keys()
    host = urllib.parse.urlparse(ENDPOINT).netloc
    now = dt.datetime.now(dt.timezone.utc)
    amz, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{date}/{REGION}/s3/aws4_request"
    query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{ak}/{scope}", "X-Amz-Date": amz,
             "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    q = "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(v, safe='~')}" for k, v in sorted(query.items()))
    canon = "\n".join(["GET", _uri(bucket, key), q, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    sig = hmac.new(_signing_key(sk, date), sts.encode(), hashlib.sha256).hexdigest()
    return f"{ENDPOINT}{_uri(bucket, key)}?{q}&X-Amz-Signature={sig}"


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "put":
        put(argv[1], argv[2], argv[3])
    elif cmd == "presign":
        print(presign(argv[1], argv[2], int(argv[3]) if len(argv) > 3 else 604800))
    elif cmd == "ls":
        st, body = _signed_request("GET", argv[1], query={"list-type": "2"})
        print(st, body.decode()[:4000])
    elif cmd == "rm":
        st, _ = _signed_request("DELETE", argv[1], argv[2])
        print("DELETE", st)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
