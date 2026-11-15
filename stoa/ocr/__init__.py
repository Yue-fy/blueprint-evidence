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

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
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
# S0 16:48: a cold OCR of a big render ran 10+ min. Over this budget the reading fails and the caller falls back (the raster
# text layer then uses the VLM reading). STOA_TEXT_CACHE=0 skips the memory and disk caches (cold-latency gate runs).
TIMEOUT = float(os.environ.get("STOA_OCR_TIMEOUT", "120") or 120)


def _cache_on() -> bool:
    return os.environ.get("STOA_TEXT_CACHE", "1") != "0"
_locks: dict[str, threading.Lock] = {}
_glock = threading.Lock()
_server_ok: bool | None = None


def _abspath(path: str) -> Path:
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    return Path(os.environ.get("STOA_DATA", ".")) / path


def _tiled_tag(path: str) -> str:
    """'|t2' for images the engine tiles (> 4000 px: engine.MAX_SIDE): their cached reads predate the clipped-read
    dedupe (O1 c6), so they are read once more; smaller images keep their cache."""
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(_abspath(path)) as im:
            return "|t2" if max(im.size) > 4000 else ""
    except Exception:  # noqa: BLE001
        return ""


def _key(path: str) -> Path:
    from stoa.solvers.raster.textlayer import file_tag  # '' for dataset files, content hash for uploads

    h = hashlib.sha1(f"{VERSION}{_tiled_tag(path)}|{Path(path).name}{file_tag(path)}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{Path(path).stem[:60]}.{h}.json"


def _private_ok() -> bool:
    """P1 20:41 / T2 private mode: the OCR server URL must be on this machine or our network (stoa.llm guard, when the
    checkout has it). Checked on every call (private mode may be per request); blocked -> the local engine subprocess."""
    try:
        from stoa.llm import check_private_url
    except ImportError:
        return True
    try:
        check_private_url(OCR_URL, "OCR server")
    except Exception:  # noqa: BLE001 - ExternalCallBlocked
        return False
    return True


def server_up(timeout: float = 0.5) -> bool:
    global _server_ok, _server_engine
    if not _private_ok():
        return False
    if _server_ok is None:
        try:
            with urllib.request.urlopen(OCR_URL, timeout=timeout) as r:
                _server_ok = r.status == 200
                try:
                    _server_engine = str(json.loads(r.read()).get("engine", ""))
                except Exception:  # noqa: BLE001
                    _server_engine = ""
        except OSError:
            _server_ok = False
    return _server_ok


_server_engine = ""
TILE_FIX = "+t2"  # engine.ENGINE tag of the clipped-read tile dedupe


def _server_for(path: str) -> bool:
    """Use the OCR server for this image? Tiled images (> 4000 px) only when the server runs the clipped-read dedupe
    (its engine string has TILE_FIX): an older server would cache the old reads under the new key. Otherwise the
    subprocess runs this checkout's engine."""
    return server_up() and (not _tiled_tag(path) or TILE_FIX in _server_engine)


def available() -> bool:
    return server_up() or Path(OCR_PY).exists()


def cached(path: str) -> bool:
    return str(_abspath(path)) in _mem or _key(path).exists()


BUDGET_MSG = f"OCR over its {TIMEOUT:.0f} s budget (STOA_OCR_TIMEOUT)"
# S0 18:18: a read over the budget was not remembered, so EVERY question on that file paid the budget again (a 14 MB
# .scan.png: 'OCR failed … over its 120 s budget' x5+). Now (1) the subprocess engine writes the cache itself and is
# left running past the budget (detached), so a later question finds the read; (2) a '<key>.timeout' marker makes the
# next questions skip OCR at once (-> the VLM text layer) for NEG_TTL seconds unless the cache has landed meanwhile.
NEG_TTL = float(os.environ.get("STOA_OCR_NEG_TTL", "1800") or 0)


SERVER_BG_TIMEOUT = float(os.environ.get("STOA_OCR_SERVER_BG", "1800") or 1800)


def _write_atomic(f: Path, d: dict) -> None:
    try:
        tmp = f.with_name(f".{f.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, f)
    except OSError:
        pass


# O1 c10 (E1 20:49, F5 21:08): the server reads one image at a time, so a small crop (E1's area-table OCR, ~1000x1500 px,
# 6 s on its own) queued behind whole-sheet reads and hit the 120 s budget -> no table for 30 min (the .timeout marker).
# A small image waits SMALL_WAIT s for the server, then runs the engine itself (~7.5 s incl. the model load); whichever
# lands first writes the cache (atomic replace, same reading).
SMALL_PX = float(os.environ.get("STOA_OCR_SMALL_MP", "3") or 0) * 1e6
SMALL_WAIT = float(os.environ.get("STOA_OCR_SMALL_WAIT", "6") or 6)


def _pixels(path: str) -> int:
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(_abspath(path)) as im:
            return im.size[0] * im.size[1]
    except Exception:  # noqa: BLE001
        return 0


def _run(path: str, cache: Path | None = None) -> dict:
    if _server_for(path):
        # O1 c8 (captain 20:08 audit): a server read over the budget used to be ABANDONED — the server finished it but
        # nobody kept the result, so after the negative-cache TTL the next question paid the budget again. The request
        # now continues in a daemon thread (no extra CPU: the server is computing it anyway) and writes `cache` when
        # the answer arrives; a later question (same server process: the API) finds it. O1 c10: the request also names
        # `out` = the cache file, which a server of this checkout writes itself when the read is done — so the reading
        # lands even when the asking process has exited (an eval worker, `python -m stoa.formats.warm`).
        body = {"path": path, **({"out": str(cache)} if cache is not None else {})}
        req = urllib.request.Request(OCR_URL, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        box: dict = {}

        def call():
            try:
                with urllib.request.urlopen(req, timeout=max(TIMEOUT, SERVER_BG_TIMEOUT)) as r:
                    d = json.loads(r.read())
                box["d"] = d
                if cache is not None and "error" not in d and box.get("late"):
                    _write_atomic(cache, d)
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        th = threading.Thread(target=call, daemon=True, name=f"ocr-{Path(path).name[:30]}")
        th.start()
        small = cache is not None and SMALL_PX > 0 and Path(OCR_PY).exists() and 0 < _pixels(path) <= SMALL_PX
        th.join(min(SMALL_WAIT, TIMEOUT) if small else TIMEOUT)
        if th.is_alive():
            box["late"] = True
            if "d" in box and cache is not None and "error" not in box["d"]:  # landed between join and the flag
                _write_atomic(cache, box["d"])
                return box["d"]
            if not small:
                raise RuntimeError(BUDGET_MSG + "; the server read continues for later questions")
            # a small image behind a busy server: read it here (below); the server's late answer may still land first
        elif "d" in box:
            return box["d"]
        e = box.get("e")
        if isinstance(e, TimeoutError) or "timed out" in str(e):
            raise RuntimeError(BUDGET_MSG)
        # any other server error: fall through to the subprocess
    env = {**os.environ, "OMP_NUM_THREADS": os.environ.get("STOA_OCR_THREADS", "8")}
    if cache is not None:
        with open(cache.with_suffix(".err"), "w") as errf:
            proc = subprocess.Popen([OCR_PY, str(ENGINE_FILE), path, "--out", str(cache)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=errf, start_new_session=True)
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:  # left running: it writes `cache` when done
            raise RuntimeError(BUDGET_MSG + "; the read continues in the background for later questions") from None
        if cache.exists():
            try:
                return json.loads(cache.read_text())
            except ValueError:  # never: both writers replace the file atomically
                pass
        raise RuntimeError("OCR engine failed: " + cache.with_suffix(".err").read_text()[-400:])
    try:
        out = subprocess.run([OCR_PY, str(ENGINE_FILE), path], capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(BUDGET_MSG) from None
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise RuntimeError(f"OCR engine failed: {out.stderr[-400:]}")
    return json.loads(lines[-1])


def read(path: str) -> dict:
    p = str(_abspath(path))
    with _glock:
        lk = _locks.setdefault(p, threading.Lock())
    with lk:
        if p in _mem and _cache_on():
            return _mem[p]
        f = _key(path)
        mark = f.with_suffix(".timeout")
        if not f.exists() and _cache_on() and NEG_TTL > 0 and mark.exists() and \
                time.time() - mark.stat().st_mtime < NEG_TTL:
            raise RuntimeError(f"OCR skipped: this file's read went over its {TIMEOUT:.0f} s budget "
                               f"{time.time() - mark.stat().st_mtime:.0f} s ago (a background read may still land)")
        if f.exists() and _cache_on():
            d = json.loads(f.read_text())
        else:
            # S0 16:48 (gate d17): 3 eval workers OCR'd the same 135-dpi render at once for 10+ min. One OCR per file
            # across processes: the others wait on the lock and then read the cache it wrote.
            import fcntl

            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f.with_suffix(".lock"), "w") as lockf:
                fcntl.flock(lockf, fcntl.LOCK_EX)
                try:
                    if f.exists() and _cache_on():
                        d = json.loads(f.read_text())
                    elif _cache_on() and NEG_TTL > 0 and mark.exists() and \
                            time.time() - mark.stat().st_mtime < NEG_TTL:
                        # O1 c10 (X1 21:32: first answers on a cold 2-page scanned PDF took 486-501 s): a question that
                        # WAITED on this lock while another process's read went over the budget used to start its own
                        # read and wait the whole budget again (serial 120 s waits per worker and page). The marker the
                        # other process just left counts here too (the background read may still land).
                        raise RuntimeError(f"OCR skipped: this file's read went over its {TIMEOUT:.0f} s budget "
                                           f"{time.time() - mark.stat().st_mtime:.0f} s ago (a background read may "
                                           "still land)")
                    else:
                        try:
                            d = _run(p, f if _cache_on() else None)
                        except RuntimeError as e:
                            if "budget" in str(e):
                                try:
                                    mark.touch()
                                except OSError:
                                    pass
                            raise
                        if "error" in d:
                            raise RuntimeError(d["error"])
                        try:
                            f.write_text(json.dumps(d))
                        except OSError:
                            pass
                finally:
                    fcntl.flock(lockf, fcntl.LOCK_UN)
        # O1 c12: short vertical dimension strings read upside down -> re-read turned (sidecar cache; vfix.py)
        from stoa.ocr import vfix

        d = vfix.apply(path, d)
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


VOCAB = os.environ.get("STOA_O1_VOCAB", "1") != "0"
_VTOK = re.compile(r"[A-Za-z]{5,}")
_GLUED = re.compile(r"\b([A-Z]{2,5})(\d{1,3})\b")
_INT_AREA = re.compile(r"^\s*\d{1,4}\s?(m²|m2)\s*$", re.I)
_INT_AREA_MISREAD = re.compile(r"^\s*(\d{1,4})\s?m[1lI|][2²]\s*$")


def _dl1(a: str, b: str) -> bool:
    """Damerau-Levenshtein distance exactly 1 (one substitution, insertion, deletion or adjacent swap)."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        d = [k for k in range(len(a)) if a[k] != b[k]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, t = (a, b) if len(a) < len(b) else (b, a)
    k = 0
    while k < len(s) and s[k] == t[k]:
        k += 1
    return s[k:] == t[k + 1:]


def _respell(tok: str, word: str) -> str:
    """`tok` spelled as `word`, in tok's own letter case. A token that is the word plus ONE letter at either end
    ('SCALEA', 'SHEETC' of 'SHEETC8', 'OFRAME') is the word and a separate letter run together: split, never drop it
    (a doubled letter, 'STANDD' / 'EEXISTING', is a stutter: dropped)."""
    lo, w = tok.lower(), word.lower()
    head = tail = ""
    if len(lo) == len(w) + 1 and lo.startswith(w) and lo[-1] != w[-1]:
        tail = " " + tok[-1]
    elif len(lo) == len(w) + 1 and lo.endswith(w) and lo[0] != w[0]:
        head = tok[0] + " "
    core = tok[1:] if head else tok[:-1] if tail else tok
    if core.isupper():
        out = w.upper()
    elif core.islower():
        out = w
    elif core[:1].isupper() and core[1:].islower():
        out = w.capitalize()
    else:
        out = word
    return f"{head}{out}{tail}"


def sheet_vocab(items: list) -> tuple[list, int]:
    """O1 c14 (X1 00:27): on X1's EFA colour-sheet scans the OCR FOUND every room tag but misread a few of a repeated
    word ('Engilsh 3', 'Eaglish 9' among 'English 1..8'), glued a tag family's number ('MFL1' / 'MFL3' among
    'MFL 2' / 'MFL 5') and read one '55 m2' as '55m12' -> area_tags summed 6 of 9 English rooms (330 for 495). The
    sheet itself says what the word is. Three consensus repairs, the reading's own text kept in nothing but the line:
    (1) a token of >= 5 letters that is no dictionary word, printed <= 2 times, one edit (Damerau) from exactly one
        dictionary word printed >= 3 times as often on the sheet -> that word (its most common spelling);
    (2) 'ABC1' -> 'ABC 1' when the sheet prints 'ABC <number>' with a space >= 2 times;
    (3) an integer 'NN m12' -> 'NN m2' on a sheet with >= 3 clean integer 'NN m2' labels.
    Without a word list (1) is off. STOA_O1_VOCAB=0 turns all three off. -> (items, number of lines changed)."""
    if not VOCAB or len(items) < 5:
        return items, 0
    from collections import Counter

    from stoa.ocr import quality

    W = quality.words()
    texts = [i.text for i in items]
    cnt = Counter(t.lower() for s in texts for t in _VTOK.findall(s))
    forms: dict[str, Counter] = {}
    for s in texts:
        for t in _VTOK.findall(s):
            forms.setdefault(t.lower(), Counter())[t] += 1
    frequent = [w for w, c in cnt.items() if c >= 3 and W is not None and w in W]
    fix: dict[str, str] = {}
    if W is not None and frequent:
        for w, c in cnt.items():
            if c > 2 or w in W:
                continue
            cands = [f for f in frequent if cnt[f] >= 3 * c and _dl1(w, f)]
            if len(cands) == 1:
                fix[w] = forms[cands[0]].most_common(1)[0][0]
    spaced = Counter(m.group(1) for s in texts for m in re.finditer(r"\b([A-Z]{2,5}) \d{1,3}\b", s))
    n_int = sum(1 for s in texts if _INT_AREA.match(s))
    out, n = [], 0
    for i in items:
        t = i.text
        if fix:
            t = _VTOK.sub(lambda m: _respell(m.group(0), fix[m.group(0).lower()]) if m.group(0).lower() in fix
                          else m.group(0), t)
        t = _GLUED.sub(lambda m: f"{m.group(1)} {m.group(2)}" if spaced[m.group(1)] >= 2 else m.group(0), t)
        if n_int >= 3:
            m = _INT_AREA_MISREAD.match(t)
            if m:
                t = f"{m.group(1)} m2"
        if t != i.text:
            i = type(i)(text=t, box=i.box, tile=getattr(i, "tile", 0))
            n += 1
        out.append(i)
    return out, n


def charge(d: dict) -> None:
    """Bill one reading (like the VLM layer: once per process, also when read from the disk cache = cold-run
    accounting; 0 GPU, 0 LLM tokens). P1 20:57: a reading with the engine's measured `cpu_s` (all threads) is charged
    exactly that through stoa.cost.record_cpu; older cached readings (no cpu_s) or a checkout without record_cpu keep
    record_cost(cpu_seconds=<engine wall seconds>) as before."""
    try:  # P1 (X1 23:27): one reading of the whole FILE; its cost is marked prep_* (stoa.cost.file_reading)
        from stoa.cost import file_reading
    except ImportError:
        file_reading = contextlib.nullcontext
    with file_reading():
        _charge(d)


def _charge(d: dict) -> None:
    cpu = d.get("cpu_s")
    if cpu:
        try:
            from stoa.cost import record_cpu
        except ImportError:
            record_cpu = None
        if record_cpu is not None:
            record_cpu(cpu)
            return
    from stoa.contract import record_cost

    record_cost(cpu_seconds=float(d.get("seconds", 0.0)))


def text_layer(path: str, min_score: float = MIN_SCORE):
    """The OCR reading as a raster TextLayer (items in original-image px, 0 LLM tokens). Wall/CPU time is the
    engine's own `seconds` (charged once, when the sheet is first read; the eval measures wall time itself)."""
    from stoa.solvers.raster.textlayer import TextItem, TextLayer

    d = read(path)
    from stoa.ocr.engine import fix_text  # idempotent; re-applied so cached layers get newer fixes

    items = [TextItem(text=fix_text(i["text"]), box=[float(v) for v in i["box"]], tile=0) for i in d["items"]
             if i.get("score", 1.0) >= min_score]
    items, n_fix = area_units(items)
    read_items = items
    items, n_voc = sheet_vocab(items)
    tl = TextLayer(path=path, size=tuple(d["size"]), items=items, tiles=1, calls=0, tokens_in=0, tokens_out=0,
                   seconds=float(d.get("seconds", 0.0)))
    if n_fix:
        tl.notes.append(f"{n_fix} room-area label(s) read as m³ / m on a sheet of m² labels: taken as m²")
    if n_voc:
        tl.read_items = read_items  # as read: layer choices (textlayer._richness) judge the reading, not the repair
        tl.notes.append(f"{n_voc} OCR line(s) spelled as the sheet prints that word elsewhere ('Engilsh 3' among "
                        "'English 1..8', 'MFL1' among 'MFL 2')")
    charge(d)
    nv = sum(1 for i in d["items"] if i.get("rot"))
    tl.notes.append(f"text read by {d.get('engine', 'PP-OCRv6')} on CPU ({len(items)} lines"
                    + (f", {nv} vertical" if nv else "") + ")")
    return tl


def verdict(path: str, min_score: float = MIN_SCORE):
    """stoa.ocr.quality.Verdict of this file's (cached) OCR reading: garbled = worth a VLM re-read (O1 c8)."""
    from stoa.ocr import quality

    d = read(path)
    return quality.judge([i for i in d["items"] if i.get("score", 1.0) >= min_score])


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
