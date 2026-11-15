"""G1 c6: 'How many doors / windows …' on an UPLOADED drawing = V1's door/window symbol detector (a small Faster R-CNN,
experiments/v1/det_server.py, served on CPU at $STOA_DET_URL, default http://127.0.0.1:8140), applied with V1's
measured recipe (18:01): render the sheet at 150 dpi, downscale to ~50 px per metre of building, keep boxes with
score >= 0.5 whose longest side is >= 0.6 m (0.75 m on imperial sheets; smaller boxes are furniture / ceiling tags).
On X1's AFCEE A-101 this finds exactly the 13 plan doors (0 on the RCPs / axons).

Scope: a question that names a viewport ('the administration module (module 02)', 'the entry vestibule module')
counts only the boxes of the drawing whose printed TITLE ('MODULE 02-ADMINISTRATION - FLOOR PLAN') matches those
words best; each box belongs to the nearest title printed below it (viewport titles sit under their drawing).
A double door detected as one symbol counts once (captain's convention: a pair = 1 door).

Abstains (the other readers run) when: not an upload, the service is down or slow, no scale, double-door / door-type
questions, or the named scope matches no title. Cost: CPU only, 15-80 s per sheet on the shared machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from stoa.contract import Answer, Evidence
from stoa.solvers import register
from stoa.solvers.base import BaseSolver

_IMG = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
DPI = 150
MAX_BOXES = 200  # evidence marks per count answer (T1: one mark per counted symbol; above this the answer says so)
TARGET_PX_PER_M = float(os.environ.get("STOA_DET_PXM", "46"))  # V1: 45-60; 46 = A-101 13/13
_Q = re.compile(r"\bhow many\s+(?:\w+\s+){0,2}?(doors?|windows?|door\s*ways?|doorways?)\b|"
                r"\bnumber of\s+(doors|windows)\b|\bcount the\s+(doors|windows)\b", re.I)
_SKIP = re.compile(r"\b(double|pairs?|single|swing|sliding|bi-?fold|revolving|types?|kinds?|fire|rated|automatic|"
                   r"exterior|external|interior|internal|glazed|wider|narrower|width|schedule|tag|leaves|leaf|open\w*|"
                   r"skylights?|roof|clerestory|elevation|facade|façade|section)\b", re.I)
# DR 22:22 (X1 22:05, DfE 'How many cupboard doors are drawn on the plan?' gold 13 -> 83 = every door): a KIND of door
# named right before 'door(s)' is a class the detector cannot tell apart ('doors in the store' is a scope, not a kind)
_KIND_DOOR = re.compile(r"\b(?:cupboard|cubicle|store|storage|wc|toilet|stall|closet|wardrobe|cabinet|louv(?:re|er)e?d|"
                        r"folding|overhead|roll-?up|coiling|garage|access|pocket|barn|french|patio|entrance|entry|"
                        r"front|back|main|emergency|escape|exit|security|steel|timber|wooden|glass)\s+doors?\b", re.I)
_TITLE = re.compile(r"\b(PLAN|RCP|AXON\w*|ELEVATION|SECTION|DETAIL|GRUNDRISS|POHJA|PLAN DE|PIANTA|PLATTEGROND)\b", re.I)
_STOP = set("""how many doors door windows window doorways doorway are there is in on the a an does do have has of this
that these those plan plans sheet drawing drawn shown show total altogether all count number what which inside within
at for to with and or its it's their be""".split())


def _det_url() -> str:
    return os.environ.get("STOA_DET_URL", "http://127.0.0.1:8140").rstrip("/")


_CACHE: dict = {}
_FAILED: dict = {}  # key -> time before which a failed / slow read is not retried (S0 18:31, V1 18:54)


def _detect(img: str, zoom: float, timeout: float) -> dict | None:
    key = (img, os.path.getmtime(img), round(zoom, 3))
    if key in _CACHE:  # several questions on one upload: one detector run
        return _CACHE[key]
    if time.time() < _FAILED.get(key, 0.0):
        return None
    res = _detect_once(img, zoom, timeout)
    if res and "boxes" in res:
        if len(_CACHE) > 64:
            _CACHE.clear()
        _CACHE[key] = res
    else:
        # V1 18:54: the service finishes and caches a run its caller gave up on -> retry a TIMEOUT after 90 s;
        # a service that is down / erroring: 10 min
        _FAILED[key] = time.time() + (90 if _up() else 600)
    return res


def _detect_once(img: str, zoom: float, timeout: float) -> dict | None:
    try:  # P1 20:41 (b): private mode never sends the drawing to a detector off this server / network
        from stoa.llm import check_private_url
    except ImportError:  # P1's guard not merged yet
        check_private_url = None
    if check_private_url is not None:
        try:
            check_private_url(_det_url(), "door detector")
        except Exception:  # noqa: BLE001 - blocked: abstain, the other readers answer
            return None
    req = urllib.request.Request(_det_url() + "/detect", data=json.dumps(
        {"path": img, "zoom": zoom, "min_score": 0.5}).encode(), headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read())
    except Exception:  # noqa: BLE001 - down / slow / error: the other readers answer
        return None
    try:  # P1 20:41 (a): the detector's 16-thread CPU time is charged to the question (0 on its cache hit)
        from stoa.cost import record_service

        record_service((res or {}).get("infer_seconds") or 0, threads=16)
    except Exception:  # noqa: BLE001 - P1's meter not merged yet
        pass
    return res


def _up() -> bool:
    try:
        with urllib.request.urlopen(_det_url() + "/health", timeout=2) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _render(pdf: str, dpi: int = DPI) -> tuple[str, object]:
    """page 0 rendered at dpi (display orientation) into a per-file cache (atomic write) -> (png, page derotation)."""
    import pymupdf

    st = os.stat(pdf)
    key = hashlib.sha1(f"{pdf}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "stoa_det"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{key}.{dpi}.png"
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        derot = page.derotation_matrix
        if not out.exists():
            tmp = d / f"{key}.{os.getpid()}.{time.time_ns()}.tmp.png"
            page.get_pixmap(dpi=dpi).save(str(tmp))
            os.replace(tmp, out)  # P1 17:56: never let another worker open a half-written file
    return str(out), derot


def _titles(pdf: str) -> list[tuple[str, tuple[float, float, float, float]]]:
    """viewport titles ('MODULE 02-ADMINISTRATION - FLOOR PLAN') in DISPLAY points (rotation applied)."""
    import pymupdf

    from stoa.solvers.label_count import text_lines

    out = []
    with pymupdf.open(pdf) as doc:
        rm = doc[0].rotation_matrix
    for t, bb in text_lines(pdf, os.stat(pdf).st_mtime):
        if _TITLE.search(t) and len(t.split()) >= 2 and len(t) <= 90 and not re.search(r"\b(SCALE|NOTE|SEE)\b", t, re.I):
            r = pymupdf.Rect(bb) * rm
            out.append((t, (r.x0, r.y0, r.x1, r.y1)))
    return out


def _tokens(s: str) -> set[str]:
    s = s.lower().replace("-", " ").replace("/", " ")
    toks = set()
    for w in re.findall(r"[a-zäöüåéèà]+|\d+", s):
        if w.isdigit():
            toks.add(str(int(w)))
        elif w not in _STOP and len(w) > 2:
            toks.add(w.rstrip("s") if len(w) > 4 else w)
    return toks


def _scope_title(question: str, titles: list) -> tuple[int | None, set[str], str]:
    """(index of the title the question names, the scope words, why); index None = whole sheet or no match."""
    q = _Q.sub(" ", question)
    words = _tokens(q) - {"floor", "level", "storey", "building"}
    words -= {"module", "wing", "area", "zone", "part", "unit", "block"} if len(words) > 1 else set()
    if not words:
        return None, set(), "whole sheet"
    best, score = None, 0.0
    for i, (t, _) in enumerate(titles):
        tt = _tokens(t)
        common = words & tt
        if not any(not w.isdigit() for w in common):
            continue  # a bare number ('English 1' vs 'MODULE 01') names no viewport
        s = len(common) + (0.5 if re.search(r"\bPLAN\b", t, re.I) else 0.0)
        if s > score:
            best, score = i, s
    if best is None:
        return None, words, "no viewport title names " + ", ".join(sorted(words))
    return best, words, f"viewport '{titles[best][0]}'"


def _owner(box_pt: tuple, titles: list) -> int | None:
    """index of the nearest title printed BELOW the box (display points), favouring vertical nearness."""
    cx, cy = (box_pt[0] + box_pt[2]) / 2, (box_pt[1] + box_pt[3]) / 2
    best, cost = None, float("inf")
    for i, (_, r) in enumerate(titles):
        if r[1] < cy:
            continue
        dx = 0.0 if r[0] - 50 <= cx <= r[2] + 400 else min(abs(cx - r[0]), abs(cx - r[2]))
        # titles start at their drawing's left edge: a box right of a title's end leans to the next title
        c = (r[1] - cy) + 1.5 * dx + 0.3 * max(0.0, cx - r[2])
        if c < cost:
            best, cost = i, c
    return best


@register("door_detect")
class DoorDetect(BaseSolver):
    sources = ()

    def can_handle(self, path: str, question: str) -> bool:
        from stoa.data import dataset_rel

        return str(path).lower().endswith((".pdf", *_IMG)) and not str(dataset_rel(path)).startswith("data/") and \
            bool(_Q.search(question))

    def solve(self, path: str, question: str) -> Answer:
        from stoa.data import abspath

        m = _Q.search(question)
        if not m:
            return self.abstain("not a 'how many doors / windows' question")
        if _SKIP.search(question) or _KIND_DOOR.search(question):
            return self.abstain("door / window TYPES or a qualifier the symbol detector cannot tell apart")
        want = "window" if "window" in (m.group(1) or m.group(2) or m.group(3) or "").lower() else "door"
        p = str(abspath(path))
        pdf = p if p.lower().endswith(".pdf") else None
        if pdf is None:
            from stoa.solvers.label_count import _render_source

            src = _render_source(p)
            pdf = src[0] if src else None
        if pdf is not None:
            try:
                from stoa.router.recognize import _pdf

                if _pdf(pdf, os.stat(pdf).st_mtime)["bim_fill"] >= 2:
                    return self.abstain("an IfcConvert-style sheet: its own door / window readers count them")
            except Exception:  # noqa: BLE001
                pass
        else:
            from stoa.solvers.label_count import _image_lines

            if _image_lines(p) is None:  # never start a slow reading here (S0 16:48); raster reads it
                return self.abstain("the image's text layer (scale) has not been read yet")
        if not _up():
            return self.abstain(f"the door/window detector service ({_det_url()}) is not running")
        t0 = time.perf_counter()
        derot = None
        if pdf is not None:
            from stoa.agent.vision import _pdf_scale

            sc = _pdf_scale(pdf)
            if not sc:
                return self.abstain("no printed scale: the symbol size filter needs one")
            n, scale_src = float(sc[0]), str(sc[1])
            try:  # '1:200 @ A1' printed on an A3 page = 1:400 on this file (line_rooms / E1 / P1 rule)
                import pymupdf

                from stoa.evidence import linework as L
                from stoa.solvers.line_rooms import paper_rescale

                with pymupdf.open(pdf) as doc:
                    r0 = doc[0].rect
                n2, note = paper_rescale(L.text_lines(pdf, 0), max(r0.width, r0.height) * 25.4 / 72, n)
                if note and n2 != n:
                    n, scale_src = n2, f"{scale_src}; {note}"
            except Exception:  # noqa: BLE001
                pass
            # small scales (1:400 school sheets) need a sharper render, not an upscale: ~TARGET px/m at zoom <= 1
            dpi = int(min(300, max(DPI, round(TARGET_PX_PER_M * 0.0254 * n))))
            img, derot = _render(pdf, dpi)
            px_per_m = dpi / 0.0254 / n
            k = dpi / 72.0
            scale_txt = f"1:{n:g} ({scale_src}), rendered at {dpi} dpi"
        else:
            from stoa.agent.vision import _image_bar

            bar = _image_bar(p)
            if bar is None or not getattr(bar, "px_per_m", None):
                return self.abstain("no scale on the image (scale bar / printed 1:N with a known sheet size)")
            img, px_per_m, k = p, float(bar.px_per_m), None
            scale_txt = f"{px_per_m:.0f} px per metre from the image's scale"
        imperial = bool(pdf is not None and n % 12 == 0 and n in (12, 16, 24, 32, 48, 64, 96, 128, 192, 384))
        min_m = 0.75 if imperial else 0.6
        zoom = max(0.15, min(1.5, TARGET_PX_PER_M / px_per_m))
        # W6 19:37: a whole-sheet photo waited 150 s at load ~290 and abstained anyway; 40 s keeps a UI question bounded
        res = _detect(img, zoom, float(os.environ.get("STOA_DET_TIMEOUT", "40")))
        if not res or "boxes" not in res:
            return self.abstain("the detector did not answer in time")
        boxes = [b for b in res["boxes"] if b[5] == want and b[4] >= 0.5 and
                 max(b[2] - b[0], b[3] - b[1]) / px_per_m >= min_m]
        titles = _titles(pdf) if pdf is not None else []
        idx, words, why = _scope_title(question, titles)
        room = None
        if words and idx is None and pdf is not None and os.environ.get("STOA_DET_ROOMS", "0") == "1":
            # counts inside a NAMED ROOM ('windows of English 1'): boxes touching its traced outline. UNMEASURED (the
            # service restarted mid-test at 18:37; a 1:400 A3 sheet needs a clip render to stay under the timeout):
            # off by default, for DR (captain 18:27: door/window counts) to measure
            room = _room_outlines(pdf, question, n)
            if isinstance(room, str):
                return self.abstain(f"{why}; {room}")
            why = "room " + " + ".join(f"'{t}'" for t, _ in room)
        elif words and idx is None:
            return self.abstain(why)
        if idx is not None:
            own = [b for b in boxes if _owner(tuple(c / k for c in b[:4]), titles) == idx]
            boxes = own
        if room is not None:
            import pymupdf
            from shapely.geometry import box as sbox

            pad = 0.25 / (n * 0.0254 / 72)  # 0.25 m in points: a window / door sits IN the room's wall
            outl = [poly.buffer(pad) for _, poly in room]
            boxes = [b for b in boxes if any(o.intersects(sbox(*_norm(pymupdf.Rect(*(c / k for c in b[:4])) * derot)))
                                             for o in outl)]
        ev = []
        for i, b in enumerate(boxes[:MAX_BOXES]):  # T1 (audit 19:51): was [:60] (DfE PNG: 84 doors, 60 marks)
            note = f"{want} {i + 1} (score {b[4]:.2f})"
            if p.lower().endswith(".pdf"):
                import pymupdf

                r = pymupdf.Rect(*(c / k for c in b[:4])) * derot
                ev.append(Evidence(file=str(path), page=0, bbox=[round(r.x0, 1), round(r.y0, 1), round(r.x1, 1),
                                   round(r.y1, 1)], bbox_space="pdf_pt", note=note, exact=False))
            else:  # the image itself, or our render of a PDF ('<stem>.r132.png': boxes are on the 150-dpi render)
                f = _img_k(p, k * 72.0) if pdf is not None else 1.0
                ev.append(Evidence(file=str(path), bbox=[round(c * f, 1) for c in b[:4]], bbox_space="image_px",
                                   note=note, exact=False))
        conf = 0.6 if pdf is not None else 0.45
        if idx is not None:
            conf -= 0.1
        secs = round(time.perf_counter() - t0, 2)
        return Answer(
            value=len(boxes), unit="count", confidence=round(conf, 2), evidence=ev,
            assumptions=[f"{want}s counted as drawn symbols (symbol detector), each symbol once: a double door = 1 door",
                         f"symbols smaller than {min_m} m at {scale_txt} are not counted (furniture, tags)"]
            + ([f"only the {why} (each symbol belongs to the nearest title printed below it)"] if idx is not None
               else []),
            missing=["a door / window schedule would confirm the count"]
            + ([f"showing {MAX_BOXES} of {len(boxes)} detected symbols: the rest are not marked"]
               if len(boxes) > MAX_BOXES else []),
            trace=[f"door_detect: {len(res['boxes'])} raw symbols, {len(boxes)} {want}s kept (score >= 0.5, >= "
                   f"{min_m} m) at zoom {zoom:.2f}; {why}; {secs} s"],
            solver="door_detect")


def _norm(r) -> tuple[float, float, float, float]:
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _room_outlines(pdf: str, question: str, n: float):
    """[(label, shapely polygon in unrotated pdf_pt)] of the room(s) the question names ('English 1'; 'English 1,
    English 2 and English 3' -> each), traced by E1's linework.measure_room at the printed scale; or why not (str)."""
    try:
        from shapely.geometry import Polygon

        from stoa.evidence import linework as L
        from stoa.solvers.line_rooms import _label
    except Exception:  # noqa: BLE001
        return "the room tracer is not available"
    lines = L.text_lines(pdf, 0)
    if len(lines) > 1500:
        return "a dense sheet: rooms are not traced"
    parts = [x for x in re.split(r",|\band\b|\bund\b|\bja\b|\bet\b|&|\+", question) if x.strip()]
    m0 = re.search(r"([A-Za-zÄÖÜäöüå][\w/]*)\s+\d{1,3}\s*$", parts[0].strip(" ?.!"))
    stem = m0.group(1) if m0 else None
    asks = []
    for part in parts:
        m = re.search(r"([A-Za-zÄÖÜäöüå][\w/]*(?:\s+[A-Za-zÄÖÜäöüå][\w/]*)*)?\s*(\d{1,3})?\s*$", part.strip(" ?.!"))
        if part is parts[0] or not m or not m.group(2) or (m.group(1) and len(m.group(1).split()) > 1):
            asks.append(part)
            continue
        asks.append(part if m.group(1) else f"{stem or ''} {m.group(2)}")  # 'English 1, 2 and 3'
    out = []
    for a in asks if len(asks) > 1 else [question]:
        lab, why = _label(a, lines)
        if lab is None or why == "same":
            return why or "the room is not named on the sheet"
        text, bb = lab
        r = L.measure_room(pdf, 0, ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), n, tag_box=tuple(bb))
        if not r or not r.get("polygon"):
            return f"no closed outline around '{text}' at 1:{n:g}"
        out.append((text, Polygon(r["polygon"])))
    if len({t for t, _ in out}) < len(out):
        return "the same room named twice"
    return out


def _img_k(img_abs: str, dpi: float = DPI) -> float:
    """image px per detector-render px for our render '<stem>.r<dpi>.png' of the PDF."""
    m = re.search(r"\.r(\d{2,4})\.png$", img_abs)
    return (int(m.group(1)) / dpi) if m else 1.0
