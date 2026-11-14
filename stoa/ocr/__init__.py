"""OCR text layer for raster drawings: PP-OCRv6 line boxes at 0 LLM tokens (O1, Sat 14 Nov).

The raster solver's text layer (`stoa/solvers/raster/textlayer.py`) was read by the shared VLM tile by tile:
25-100k tokens and ~3 min of shared GPU per cold HABS sheet, and boxes that are guesses for small print. This module
reads the same layer with PP-OCRv6 (PaddleOCR 3.7, Apache-2.0) on CPU: ~30 s per 3000 px sheet with 8 threads,
pixel-exact line boxes, and vertical margin lines (HABS title blocks) re-read on a rotated crop.

PaddleOCR runs in its own env (see engine.py for why), so this module never imports paddle. The order is
memory → disk cache ($JUNCTION_SHARED/artifacts/O1/ocr) → OCR server ($STOA_OCR_URL, default :8110) → a one-off
subprocess in $STOA_OCR_PYTHON. `available()` is False when none of the last two exist; the caller then keeps the
VLM layer.

    read(path)        -> {"size", "items": [{"text", "box", "score", "rot"}], "seconds", "engine"}
    text_layer(path)  -> raster TextLayer (same items/boxes contract as the VLM layer; tokens 0)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

OCR_PY = os.environ.get("STOA_OCR_PYTHON", ".venv-ocr/bin/python")
OCR_URL = os.environ.get("STOA_OCR_URL", "http://127.0.0.1:8110")
ENGINE_FILE = Path(__file__).with_name("engine.py")
CACHE_DIR = Path(os.environ.get("JUNCTION_SHARED", ".stoa")) / "artifacts" / \
    "O1" / "ocr"
VERSION = "o2"  # bump to invalidate the disk cache
MIN_SCORE = 0.5  # recogniser score below this is mostly noise on hatching / drawn lines
_mem: dict[str, dict] = {}
_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()
_server_ok: bool | None = None


def _abspath(path: str) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return Path(os.environ.get("STOA_DATA", ".")) / path


def _key(path: str) -> Path:
    from stoa.solvers.raster.textlayer import file_tag  # '' for dataset files, content hash for uploads

    h = hashlib.sha1(f"{VERSION}|{Path(path).name}{file_tag(path)}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{Path(path).stem[:60]}.{h}.json"


def server_up(timeout: float = 0.5) -> bool:
    global _server_ok
    if _server_ok is None:
        try:
            with urllib.request.urlopen(OCR_URL, timeout=timeout) as r:
                _server_ok = r.status == 200
        except OSError:
            _server_ok = False
    return _server_ok


def available() -> bool:
    return server_up() or Path(OCR_PY).exists()


def cached(path: str) -> bool:
    return str(_abspath(path)) in _mem or _key(path).exists()


def _run(path: str) -> dict:
    if server_up():
        req = urllib.request.Request(OCR_URL, data=json.dumps({"path": path}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read())
        except OSError:
            pass  # fall through to the subprocess
    env = {**os.environ, "OMP_NUM_THREADS": os.environ.get("STOA_OCR_THREADS", "8")}
    out = subprocess.run([OCR_PY, str(ENGINE_FILE), path], capture_output=True, text=True, timeout=900, env=env)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"OCR engine failed: {out.stderr[-400:]}")
    return json.loads(lines[-1])


def read(path: str) -> dict:
    p = str(_abspath(path))
    with _glock:
        lk = _locks.setdefault(p, threading.Lock())
    with lk:
        if p in _mem:
            return _mem[p]
        f = _key(path)
        if f.exists():
            d = json.loads(f.read_text())
        else:
            d = _run(p)
            if "error" in d:
                raise RuntimeError(d["error"])
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(d))
            except OSError:
                pass
        _mem[p] = d
        return d


_AREA_LABEL = re.compile(r"^\s*(\d{1,3}[.,]\d{1,2})\s?(m²|m2|m\^2)\s*$", re.I)
_AREA_MISREAD = re.compile(r"^\s*(\d{1,3}[.,]\d{2})\s?(m³|m3|m)\s*$", re.I)


def area_units(items: list) -> tuple[list, int]:
    """W5 13:26 phone photo of a Swiss plan: OCR read room-area labels '15.61 m²' as '15.61 m³' or '10.16m' (the
    superscript lost or misread), so they stopped being areas. On a sheet with >= 3 clean 'NN.NN m²' labels, a line
    that is only 'NN.NN m³' / 'NN.NNm' (two decimals, < 1000) is one more area label. -> (items, number fixed)."""
    if sum(1 for i in items if _AREA_LABEL.match(i.text)) < 3:
        return items, 0
    out, n = [], 0
    for i in items:
        m = _AREA_MISREAD.match(i.text)
        if m:
            i = type(i)(text=f"{m.group(1)} m²", box=i.box, tile=getattr(i, "tile", 0))
            n += 1
        out.append(i)
    return out, n


def text_layer(path: str, min_score: float = MIN_SCORE):
    """The OCR reading as a raster TextLayer (items in original-image px, 0 LLM tokens). Wall/CPU time is the
    engine's own `seconds` (charged once, when the sheet is first read; the eval measures wall time itself)."""
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    d = read(path)
    from stoa.ocr.engine import fix_text  # idempotent; re-applied so cached layers get newer fixes

    items = [TextItem(text=fix_text(i["text"]), box=[float(v) for v in i["box"]], tile=0) for i in d["items"]
             if i.get("score", 1.0) >= min_score]
    items, n_fix = area_units(items)
    tl = TextLayer(path=path, size=tuple(d["size"]), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=float(d.get("seconds", 0.0)))
    if n_fix:
        tl.notes.append(f"{n_fix} room-area label(s) read as m³ / m on a sheet of m² labels: taken as m²")
    from stoa.contract import record_cost

    # billed like the VLM layer: once per process, also when read from the disk cache (cold-run accounting).
    # cpu_seconds = the engine's wall time with STOA_OCR_THREADS (8) CPU threads; 0 GPU, 0 LLM tokens.
    record_cost(cpu_seconds=float(d.get("seconds", 0.0)))
    nv = sum(1 for i in d["items"] if i.get("rot"))
    tl.notes.append(f"text read by {d.get('engine', 'PP-OCRv6')} on CPU ({len(items)} lines"
                    + (f", {nv} vertical" if nv else "") + ")")
    return tl


def index(paths: list[str], jobs: int = 3) -> None:
    """Fill the disk cache for many drawings: `jobs` engine processes in parallel (each STOA_OCR_THREADS threads),
    then the results are filed under their cache keys. Already-cached drawings are skipped."""
    import tempfile

    todo = sorted({str(_abspath(p)) for p in paths if not _key(p).exists()})
    print(f"{len(todo)} to read ({len(set(paths)) - len(todo)} cached), {jobs} jobs", flush=True)
    if not todo:
        return
    env = {**os.environ, "OMP_NUM_THREADS": os.environ.get("STOA_OCR_THREADS", "8")}
    with tempfile.TemporaryDirectory() as tmp:
        procs = [subprocess.Popen([OCR_PY, str(ENGINE_FILE), *todo[i::jobs], "--out-dir", tmp], env=env,
                                  stdout=sys.stdout, stderr=subprocess.DEVNULL) for i in range(jobs) if todo[i::jobs]]
        for pr in procs:
            pr.wait()
        for p in todo:
            src = Path(tmp) / (Path(p).name + ".json")
            if src.exists():
                d = json.loads(src.read_text())
                if "error" not in d:
                    f = _key(p)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(json.dumps(d))
