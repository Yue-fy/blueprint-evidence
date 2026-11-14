"""Auto takeoff (F2, captain 19:54): one click on a sheet -> a quantity table a site engineer can price from.

Rows: every labelled room's floor area, its baseboard (skirting) length (our convention: the room's wall-face
perimeter minus its door openings), its doors and windows; the sheet's door / window totals; slab areas by type when
the sheet calls slab types out. Every row keeps its value, unit, confidence, the reader that produced it, its
assumptions and its evidence (the same Evidence the answer card shows, so the UI's 'Show' link highlights it).

How (no new answer logic):
- the room list = the room-name labels printed on the sheet (stoa.evidence.lineplan.labels on a PDF's text layer,
  title-block / note text dropped, labels outside the plan views dropped when stoa.sheet.views finds the views);
- a room whose name is printed once -> the SAME pipeline the UI's /ask uses (web/server/ask_upload.answer, else the
  `upload` solver), one question per quantity ('What is the area of the Laundry?' ...), Gemini / Nemotron / the agent
  OFF and STOA_PRIVATE=1: the takeoff stays on our machine and on the cheap cost tier;
- a name printed several times ('Group Room' x10) -> each copy measured at its own label with E1's wall-line tracer
  (stoa.evidence.linework.measure_room, the reader lineplan uses once it has picked a label) and lineplan's skirting
  rule; doors / windows of those copies are left to the sheet totals (no per-copy question exists);
- slab types from stoa.evidence.slabs.callouts -> 'What is the area of the <callout>?' through the pipeline.

Runs as a niced background process per (file content, page, engine version) with a progress file and a disk cache
(the CPU budget: one takeoff at a time machine-wide, flock), so the API stays responsive and a second click is instant.

Router (mounted by S0 under /feat/takeoff; inert unless STOA_FEATURES lists 'takeoff'):
    POST /feat/takeoff/start   {path, page?, refresh?}  -> {job, status, done, total, ...}
    GET  /feat/takeoff/status?job=...[&path=]           -> progress + rows so far (rows complete when status 'done')
    GET  /feat/takeoff/summary?path=&page=[&start=1]    -> 'This floor at a glance' (W5's card; see summary())
    GET  /feat/takeoff/export?job=...&fmt=csv|xlsx      -> the table as a file
    POST /feat/takeoff/export  {rows, fmt, title?}      -> the same for rows the browser collected ('Add to takeoff')
CLI: python -m stoa.features.takeoff run <file> [--page N] [--virtual upload/ab12/x.pdf] [--out DIR]
     python -m stoa.features.takeoff export <result.json> --fmt csv|xlsx [--out FILE]
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ENGINE_V = "t2"  # t2: every door / window box kept (one marker per item), sheet ID per row
REPO = Path(__file__).resolve().parents[2]
QUESTION_BUDGET_S = float(os.environ.get("STOA_TAKEOFF_Q_BUDGET", "75"))
MAX_ROOMS = int(os.environ.get("STOA_TAKEOFF_MAX_ROOMS", "80"))
MAX_MARKS = 600  # a count keeps one evidence box per item it counted (the estimator's marker per door)
SOLVER_ARGS = {"gemini": 0, "verify": 0, "agent": int(os.environ.get("STOA_TAKEOFF_AGENT", "0"))}

# the questions asked per room (wording the pipeline's readers parse: lineplan _BASE_Q / openings parse_question)
ROOM_QS = (
    ("area", "What is the floor area of the {room}?", "m2"),
    ("baseboard", "How many linear metres of baseboard does the {room} need, door openings excluded?", "m"),
    ("doors", "How many doors does the {room} have?", "count"),
    ("windows", "How many windows does the {room} have?", "count"),
)
# how each quantity is asked: (solver or None = the whole cascade, detector on?, budget s). Per-room counts go to DR's
# `openings` reader alone (swing arcs: 0.3-2 s, DfE laundry 1 / kitchen 4, EFA English 1 = gold 1) with the detector
# off: through the cascade a room it cannot trace fell to the local VLM (75 s, no answer) and with the detector on
# each room waited on :8140. Windows on line drawings need that detector (one render per room, ~1 min each here), so
# per-room windows are mostly 'no reading' and the sheet total (one detector pass) carries them.
ROUTE = {"area": (None, False, 60.0), "baseboard": (None, False, 60.0), "doors": ("openings", False, 30.0),
         "windows": ("openings", False, 30.0), "slab_area": (None, False, 90.0),
         "sheet_doors": (None, True, 180.0), "sheet_windows": (None, True, 180.0), "sheet_area": (None, True, 120.0)}
SHEET_QS = (
    ("doors", "How many doors are on this plan?", "count"),
    ("windows", "How many windows are on this plan?", "count"),
)
QUANTITY_LABEL = {"area": "Floor area", "baseboard": "Baseboard (skirting)", "doors": "Doors", "windows": "Windows",
                  "slab_area": "Slab area", "rooms": "Rooms labelled", "area_sum": "Net floor area",
                  "baseboard_sum": "Skirting total", "baseboard_dry": "Skirting total without wet rooms",
                  "door_leaves": "Door leaves"}

# ------------------------------------------------------------------------------------------------ paths and caching


def uploads_dir() -> Path:
    """Where the web middleware keeps uploads (web/mock/stoaApi.ts: STOA_UPLOADS, default <tmp>/stoa-uploads)."""
    return Path(os.environ.get("STOA_UPLOADS") or Path(tempfile.gettempdir()) / "stoa-uploads")


def cache_dir() -> Path:
    d = Path(os.environ.get("STOA_TAKEOFF_CACHE") or Path(tempfile.gettempdir()) / "stoa-takeoff")
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(path: str) -> tuple[str, str]:
    """A path the browser knows -> (absolute file, virtual path). Only uploads (upload/<id>/<name>) and dataset files
    (dataset-relative); anything else raises ValueError (no reading arbitrary files through the API)."""
    path = str(path or "").strip()
    if not path or ".." in Path(path).parts or path.startswith("/"):
        raise ValueError("path must be an upload (upload/<id>/<name>) or dataset-relative, without '..'")
    if path.startswith("examples/"):  # a catalogue example sheet (web/fixtures/projects.json), for the warm-up
        p = example_abs(path)
        if p is None:
            raise FileNotFoundError(path)
        return str(p), path
    if path.startswith("upload/"):
        root = uploads_dir().resolve()
        p = (root / path[len("upload/"):]).resolve()
        if root not in p.parents:
            raise ValueError("path outside the uploads folder")
    else:
        from stoa.data import abspath, data_root

        p, root = abspath(path).resolve(), data_root().resolve()
        if root not in p.parents:
            raise ValueError("path outside the dataset")
    if not p.is_file():
        raise FileNotFoundError(path)
    return str(p), path


def example_dirs() -> list[Path]:
    """The folders the web server's POST /api/case opens catalogue sheets from (web/mock/stoaApi.ts EXAMPLE_DIRS)."""
    shared = Path(os.environ.get("JUNCTION_SHARED") or ".stoa")
    return [Path(d) for d in (os.environ.get("STOA_EXAMPLES_DIR", ""), shared / "artifacts/stoa-examples",
                              shared / "datasets/stoa-like/originals", shared / "artifacts/W6/cases",
                              shared / "datasets/stoa-like/dfe") if str(d)]


def catalogue() -> list[dict]:
    """The catalogue's sheets: [{project, label, path}] from web/fixtures/projects.json (PDF / PNG / JPEG only)."""
    try:
        ps = json.loads((REPO / "web/fixtures/projects.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [{"project": p.get("id"), "label": x.get("label"), "path": x.get("path")} for p in ps
            for x in p.get("sheets") or [] if re.search(r"\.(pdf|png|jpe?g)$", x.get("path") or "", re.I)]


def example_abs(v: str) -> Path | None:
    """'examples/<file>' of the catalogue -> the file (found by its base name, like the web server), else None."""
    if not v.startswith("examples/") or v not in {c["path"] for c in catalogue()}:
        return None
    for d in example_dirs():
        f = d / Path(v).name
        if f.is_file():
            return f.resolve()
    return None


_KEYS: dict = {}


def job_key(abs_path: str, page: int) -> str:
    """(file content, page, engine) -> the job id; the content hash is memoised by (path, size, mtime)."""
    try:
        stt = os.stat(abs_path)
        mk = (abs_path, stt.st_size, stt.st_mtime_ns, int(page))
        if mk in _KEYS:
            return _KEYS[mk]
    except OSError:
        mk = None
    key = _job_key(abs_path, page)
    if mk is not None:
        _KEYS[mk] = key
    return key


def _job_key(abs_path: str, page: int) -> str:
    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"{h.hexdigest()[:16]}-p{int(page)}-{ENGINE_V}"


def _write_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    os.replace(tmp, p)


# ------------------------------------------------------------------------------------------------------- room list
_TITLE_RX = re.compile(
    r"\b(issued|revision|rev\b|drawn|checked|approved|scale|date|project|client|title|sheet|description|originator|"
    r"size|status|suitability|coincidental|copyright|disclaimer|notes?|legend|key\s+plan|north|drawing|dwg|"
    r"plan\s+level|level\s+\d|floor\s+plan|architect|consultant|contractor|www\.|\.com|tel\b|"
    r"do\s+not\s+scale|dimensions?|all\s+rights)\b", re.I)
_NOT_ROOM_RX = re.compile(r"^(?:up|dn|down|ffl|sfl|ceiling|wall|door|window|glazing|column|beam|fire|exit|line|grid|"
                          r"typ|typical|existing|new|proposed|see|refer|note|ditto|open\s+to|below|above|over)\b", re.I)


def _clean(text: str) -> str:
    return " ".join(str(text).replace(" ", " ").split()).strip(" .,:;")


def _room_name_ok(text: str) -> bool:
    t = _clean(text)
    words = t.split()
    if not 1 <= len(words) <= 6 or len(t) > 48 or len(t) < 3:
        return False
    letters = sum(c.isalpha() for c in t)
    if letters < 3 or letters < 0.6 * sum(not c.isspace() for c in t):
        return False
    if _TITLE_RX.search(t) or _NOT_ROOM_RX.search(t) or t.endswith(("?", "!")) or str(text).rstrip().endswith("."):
        return False
    if t.startswith("(") or t.count("(") != t.count(")") and not t.endswith("(") and t.count("(") < t.count(")"):
        return False  # a label's second line on its own ('Needs)', '(Ground)'): OCR / stacking split it
    if re.search(r"\d{3,}", t) or re.search(r"[-_/]\w*\d", t) and not re.search(r"[A-Za-z]{3,}\s", t):
        return False  # sheet numbers, dates, codes ('SEN-DFE-XX-00-D-X-0201')
    return True


def _merge_open_parens(labs: list[dict]) -> list[dict]:
    """'Secondary Classroom (Severe' over 'Needs)' -> one label (lineplan.labels stacks at most similar heights)."""
    out, used = [], set()
    for i, a in enumerate(labs):
        if i in used:
            continue
        if a["text"].count("(") > a["text"].count(")"):
            ax0, ay0, ax1, ay1 = a["bbox"]
            h = max(1.0, ay1 - ay0)
            for j, b in enumerate(labs):
                if j == i or j in used or not b["text"].endswith(")"):
                    continue
                bx0, by0, bx1, by1 = b["bbox"]
                if 0 <= by0 - ay1 <= 1.5 * h and abs((ax0 + ax1) / 2 - (bx0 + bx1) / 2) <= max(ax1 - ax0, bx1 - bx0):
                    a = {**a, "text": a["text"] + " " + b["text"],
                         "bbox": [min(ax0, bx0), ay0, max(ax1, bx1), by1]}
                    a["pt"] = ((a["bbox"][0] + a["bbox"][2]) / 2, (a["bbox"][1] + a["bbox"][3]) / 2)
                    used.add(j)
                    break
        out.append(a)
    return out


def _plan_views(pdf: str, page: int) -> list:
    try:
        from stoa.sheet.views import views

        vs = [v for v in views(pdf, page) if v.bbox and v.kind in ("floor_plan", "drawing", "rcp", None)]
        return vs
    except Exception:  # noqa: BLE001 — no view reading: keep every label that looks like a room name
        return []


def room_labels(pdf: str, page: int = 0) -> list[dict]:
    """The rooms printed on a PDF sheet: [{name, key, pt, bbox, copy, copies}] (pdf_pt), in reading order."""
    from stoa.evidence import lineplan

    labs = _merge_open_parens(lineplan.labels(pdf, page))
    vs = _plan_views(pdf, page)
    out = []
    for lab in labs:
        name = _clean(lab["text"])
        if not _room_name_ok(lab["text"]):
            continue
        cx, cy = lab["pt"]
        if vs and not any(v.contains(cx, cy, pad=4) for v in vs):
            continue
        out.append({"name": name, "key": name.lower(), "pt": [round(cx, 2), round(cy, 2)],
                    "bbox": [round(v, 2) for v in lab["bbox"]]})
    counts: dict[str, int] = {}
    for r in out:
        counts[r["key"]] = counts.get(r["key"], 0) + 1
    seen: dict[str, int] = {}
    stems: dict[str, set] = {}
    for r in out:
        stems.setdefault(_stem(r["key"]), set()).add(r["key"])
    for r in out:
        seen[r["key"]] = seen.get(r["key"], 0) + 1
        r["copy"], r["copies"] = seen[r["key"]], counts[r["key"]]
        # 'Toilets' next to 'Toilet' x4: asked by name, the pipeline measured several rooms (DfE: skirting 20.03 m
        # summed over toilets, its own note 3.46 m) -> measure it at its own label like a repeated name
        r["by_label"] = r["copies"] > 1 or len(stems[_stem(r["key"])]) > 1
    return out[:MAX_ROOMS]


def _stem(key: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in key.split())


def _slab_name(text: str) -> str:
    """'300 SLAB-ON- GRADE W/ D16 @ 300 TOP FACE. BOTTOM …' -> '300 SLAB-ON-GRADE W/ D16 @ 300' (the type, no notes)."""
    t = re.sub(r"-\s+", "-", " ".join(str(text).split()))
    m = re.match(r"^(.*?\bD\d+(?:\s*@\s*\d+)?)", t)
    return (m.group(1) if m else t)[:90].strip(" .,;")


def slab_types(pdf: str, page: int = 0) -> list[dict]:
    try:
        from stoa.evidence import slabs

        return [{"text": _slab_name(c[0]), "callout": c[0][:120], "thickness_mm": c[1], "bars": list(c[2]),
                 "bbox": [round(v, 1) for v in c[4]]} for c in slabs._typed(slabs.callouts(pdf, page))]
    except Exception:  # noqa: BLE001
        return []


# ----------------------------------------------------------------------------------------------------- answering
_ASK = None


def _asker():
    """The UI's own /ask path for uploads (W5's web/server/ask_upload.answer: evidence renamed to the virtual path,
    cost meter, page on screen); the `upload` solver directly when that file is missing."""
    global _ASK
    if _ASK is not None:
        return _ASK
    srv = REPO / "web" / "server"
    try:
        if str(srv) not in sys.path:
            sys.path.insert(0, str(srv))
        import ask_upload  # type: ignore

        _ASK = ask_upload.answer
    except Exception:  # noqa: BLE001
        def _direct(req: dict) -> dict:
            from stoa.solvers import available, get_solver

            name = "upload" if "upload" in available() else "pipeline"
            ans = get_solver(name, **req.get("solver_args", {})).solve(req["abs"], req["question"])
            out = ans.model_dump()
            for e in out.get("evidence") or []:
                if not e.get("file") or Path(str(e["file"])).name == Path(req["abs"]).name:
                    e["file"] = req["virtual"]
            return out

        _ASK = _direct
    return _ASK


DEAD_DETECTOR = "http://127.0.0.1:9"  # closed port: the detector client fails at once ('service is down')


def _direct(solver: str, abs_path: str, virtual: str, question: str) -> dict:
    """One reader alone (no cascade, no VLM fallback), evidence renamed to the virtual path like ask_upload does."""
    from stoa.solvers import get_solver

    out = get_solver(solver).solve(abs_path, question).model_dump()
    for e in out.get("evidence") or []:
        if not e.get("file") or Path(str(e["file"])).name == Path(abs_path).name:
            e["file"] = virtual
    return out


def ask(abs_path: str, virtual: str, question: str, page: int, budget_s: float = QUESTION_BUDGET_S,
        solver: str | None = None, detector: bool = True) -> dict:
    """One answer as a dict, or {'status': 'timeout' | 'error', ...} (never raises). solver=None -> the UI's /ask path
    (the whole cascade); solver='openings' -> that reader alone. detector=False -> V1's CPU door/window detector is
    not called (STOA_DET_URL -> a closed port for this question: per-room questions would each wait up to 240 s on
    the service the demo shares)."""
    req = {"abs": abs_path, "virtual": virtual, "question": question, "solver_args": dict(SOLVER_ARGS)}
    if page:
        req["page"] = page
    box: dict = {}

    def run():
        try:
            box["out"] = _direct(solver, abs_path, virtual, question) if solver else _asker()(req)
        except BaseException as e:  # noqa: BLE001
            box["err"] = f"{type(e).__name__}: {e}"

    det0 = os.environ.get("STOA_DET_URL")
    if not detector:
        os.environ["STOA_DET_URL"] = DEAD_DETECTOR

    t0 = time.perf_counter()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(budget_s)
    took = round(time.perf_counter() - t0, 2)
    if not detector:
        if det0 is None:
            os.environ.pop("STOA_DET_URL", None)
        else:
            os.environ["STOA_DET_URL"] = det0
    if th.is_alive():
        return {"status": "timeout", "seconds": took, "error": f"no answer within {budget_s:.0f} s"}
    if "err" in box:
        return {"status": "error", "seconds": took, "error": box["err"]}
    out = box["out"]
    out["status"] = "ok" if out.get("value") is not None else "abstain"
    out["seconds"] = took
    return out


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def row_from_answer(base: dict, out: dict) -> dict:
    """A takeoff row from a pipeline answer: the number the card would show, its unit and evidence."""
    row = dict(base)
    row.update(status=out.get("status", "ok"), seconds=out.get("seconds"))
    if out.get("status") in ("timeout", "error"):
        row.update(value=None, unit=base.get("unit"), confidence=0.0, method="", note=out.get("error", ""),
                   evidence=[], assumptions=[])
        return row
    val = out.get("value")
    num = _num(val)
    row.update(value=num if num is not None else val, unit=out.get("unit") or base.get("unit"),
               confidence=round(float(out.get("confidence") or 0.0), 3), method=str(out.get("solver") or ""),
               evidence=[{k: e.get(k) for k in ("file", "page", "bbox", "bbox_space", "polygon", "polyline", "note",
                                                "ifc_guid") if e.get(k) is not None}
                         for e in (out.get("evidence") or [])][:MAX_MARKS if base.get("unit") == "count" else 8],
               assumptions=[str(a) for a in (out.get("assumptions") or [])][:6],
               missing=[str(m) for m in (out.get("missing") or [])][:3],
               cost=out.get("cost") or {})
    if row["unit"] in ("m²", "sqm"):
        row["unit"] = "m2"
    if row["value"] is not None and re.search(r"(?:^|/)(?:prior|dummy)$", row["method"]):
        # a typical size for the room name / question kind, nothing measured (EFA 'PE Store' 55.72 @0.007, gold 51)
        row["status"] = "estimate"
        row["note"] = "typical value for this kind of question, not read from the drawing"
    return row


def _scale(pdf: str, page: int) -> float | None:
    try:
        from stoa.evidence import linework as L

        s = L.sheet_scales(pdf, page)
        return float(s[0]) if s else None
    except Exception:  # noqa: BLE001
        return None


def measure_copy(pdf: str, virtual: str, page: int, room: dict, n: float | None) -> list[dict]:
    """Area + baseboard of ONE copy of a repeated room name, traced at its own label (E1's tracer + skirting rule)."""
    base = {"section": "room", "item": room["name"], "copy": room["copy"], "copies": room["copies"],
            "label_box": room["bbox"]}
    rows = []
    t0 = time.perf_counter()
    m = None
    if n:
        try:
            from stoa.evidence import lineplan

            m = lineplan._measure(pdf, page, tuple(room["pt"]), n, tuple(room["bbox"]))
        except Exception:  # noqa: BLE001
            m = None
    took = round(time.perf_counter() - t0, 2)
    if m is None:
        why = "no printed scale on the sheet" if not n else "its walls could not be traced around the label"
        for q, unit in (("area", "m2"), ("baseboard", "m")):
            rows.append({**base, "quantity": q, "unit": unit, "value": None, "confidence": 0.0, "status": "abstain",
                         "method": "lineplan tracer", "note": why, "evidence": [], "assumptions": [], "seconds": took})
        return rows
    poly = m["polygon"]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    bb = [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]
    conf = 0.5 if m.get("support", 0) >= 3 else 0.35
    if "bridge@" in str(m.get("variant")):
        conf = min(conf, 0.4)
    assum = [f"scale: 1:{n:g} printed on the sheet", f"copy {room['copy']} of {room['copies']} rooms labelled "
             f"'{room['name']}', outline traced from the wall lines around its own label"]
    rows.append({**base, "quantity": "area", "unit": "m2", "value": m["area_m2"], "confidence": conf, "status": "ok",
                 "method": "lineplan tracer (per label)", "seconds": took, "assumptions": assum,
                 "evidence": [{"file": virtual, "page": page, "bbox": bb, "bbox_space": "pdf_pt",
                               "polygon": [[round(x, 2), round(y, 2)] for x, y in poly],
                               "note": f"{room['name']} ({room['copy']}/{room['copies']}): traced outline "
                                       f"{m['area_m2']:.2f} m² at 1:{n:g}"}]})
    sk = None
    try:
        from stoa.evidence import lineplan

        if _no_skirting(room["name"]):
            return rows
        sk = lineplan._skirting(pdf, page, n, m)
    except Exception:  # noqa: BLE001
        sk = None
    if sk is None:
        rows.append({**base, "quantity": "baseboard", "unit": "m", "value": None, "confidence": 0.0,
                     "status": "abstain", "method": "lineplan skirting", "note": "no skirting reading for this room",
                     "evidence": [], "assumptions": [], "seconds": 0})
    else:
        n_open = len(sk.get("openings") or [])
        rows.append({**base, "quantity": "baseboard", "unit": "m", "value": sk["value_m"], "confidence": min(conf, 0.45),
                     "status": "ok", "method": "lineplan skirting (per label)", "seconds": 0,
                     "assumptions": [*assum, f"perimeter {sk['perimeter_m']:.2f} m minus {n_open} opening(s)"],
                     "evidence": [{"file": virtual, "page": page, "bbox": bb, "bbox_space": "pdf_pt",
                                   "polyline": run, "note": f"baseboard run of {room['name']} ({room['copy']}/"
                                                            f"{room['copies']})"} for run in (sk.get("runs") or [])[:6]]})
    return rows


def _det_view(pdf: str):
    """door_detect's own render / zoom for this sheet (same scale rule, so the detector run the sheet's window total
    made in this process is reused from door_detect._CACHE): (img, derot, k, px_per_m, zoom, n) or None."""
    from stoa.agent.vision import _pdf_scale
    from stoa.solvers import door_detect as D

    sc = _pdf_scale(pdf)
    if not sc:
        return None
    n = float(sc[0])
    try:
        import pymupdf

        from stoa.evidence import linework as L
        from stoa.solvers.line_rooms import paper_rescale

        with pymupdf.open(pdf) as doc:
            r0 = doc[0].rect
        n2, note = paper_rescale(L.text_lines(pdf, 0), max(r0.width, r0.height) * 25.4 / 72, n)
        if note and n2 != n:
            n = n2
    except Exception:  # noqa: BLE001
        pass
    dpi = int(min(300, max(D.DPI, round(D.TARGET_PX_PER_M * 0.0254 * n))))
    img, derot = D._render(pdf, dpi)
    px_per_m = dpi / 0.0254 / n
    return img, derot, dpi / 72.0, px_per_m, max(0.15, min(1.5, D.TARGET_PX_PER_M / px_per_m)), n


def windows_by_room(pdf: str, virtual: str, rows: list[dict], timeout: float = 20.0, page: int = 0) -> int:
    """Per-room WINDOWS from ONE detector pass over the sheet (V1's symbols; the sheet total asked it already, so
    this is a cache hit when that total came from door_detect; when it came from `openings` and :8140 is busy the
    call gives up after `timeout` -- DfE final run: 90 s waited for nothing -- and V1's service caches the run, so
    'Generate again' fills it): a window box counts for a room when it touches the room's traced outline grown by 0.25 m
    (windows sit in the room's wall; DoorDetect's own STOA_DET_ROOMS rule). Fills the 'windows' cell of every room
    whose area row has an outline and whose windows have no reading yet. Returns how many rooms were filled.
    A vector sheet that DRAWS its windows as stacked glazing lines is read from the linework first (DR's
    openings.vector_windows, < 1 s, no detector; DR 21:06); the detector pass is the fallback."""
    try:
        from shapely.geometry import Polygon
        from shapely.geometry import box as sbox
    except Exception:  # noqa: BLE001
        return 0
    wins, how = [], "door_detect symbols on the room's outline (one pass per sheet)"
    rule = "windows = the detector's window symbols touching the room's traced outline (+0.25 m)"
    conf, n = 0.45, None
    try:
        from stoa.evidence import linework as L
        from stoa.solvers import openings as O

        sc = L.sheet_scales(pdf, page)
        n0 = float(sc[0]) if sc else None
        if n0 and os.environ.get("STOA_DR_VWIN", "1") != "0":
            n = O.find_swings(pdf, page, n0).get("scale_n") or n0
            vu = O.vector_windows(pdf, page, n) or []
            if len(vu) >= 4:  # DR's own rule: fewer = this sheet does not draw glazing lines
                wins = [(sbox(*u["box"]), [round(v, 1) for v in u["box"]], 1.0) for u in vu]
                how = "window units drawn in the walls (linework) on the room's outline"
                rule = ("windows = window units drawn in the walls (2-4 parallel glazing lines sharing one span, DR's "
                        "rule) touching the room's traced outline (+0.25 m)")
                conf = 0.55
    except Exception:  # noqa: BLE001
        wins = []
    if not wins:
        try:
            import pymupdf

            from stoa.solvers import door_detect as D

            g = _det_view(pdf)
            if g is None:
                return 0
            img, derot, k, px_per_m, zoom, n = g
            res = D._detect(img, zoom, timeout)
        except Exception:  # noqa: BLE001
            return 0
        if not res or "boxes" not in res:
            return 0
        for b in res["boxes"]:
            if b[5] != "window" or b[4] < 0.5 or max(b[2] - b[0], b[3] - b[1]) / px_per_m < 0.6:
                continue
            r = pymupdf.Rect(*(c / k for c in b[:4])) * derot
            r.normalize()
            wins.append((sbox(r.x0, r.y0, r.x1, r.y1), [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1),
                                                        round(r.y1, 1)], b[4]))
    if not n:
        return 0
    pad = 0.25 / (n * 0.0254 / 72)
    by_key: dict = {}
    for r in rows:
        if r.get("section") == "room":
            by_key.setdefault((r["item"], r.get("copy", 1)), {})[r["quantity"]] = r
    filled = 0
    for (item, copy), d in by_key.items():
        a = d.get("area")
        poly = next((e.get("polygon") for e in (a or {}).get("evidence") or [] if e.get("polygon")), None)
        if not poly or len(poly) < 3:
            continue
        w = d.get("windows")
        if w is not None and w.get("value") is not None:
            continue
        try:
            outline = Polygon(poly).buffer(pad)
        except Exception:  # noqa: BLE001
            continue
        hit = [x for x in wins if outline.intersects(x[0])]
        row = {"section": "room", "item": item, "copy": copy, "copies": (a or {}).get("copies", 1),
               "quantity": "windows", "unit": "count", "value": len(hit), "confidence": conf, "status": "ok",
               "method": how, "seconds": 0,
               "assumptions": [rule + "; a window between two rooms counts for both", f"scale 1:{n:g}"],
               "evidence": [{"file": virtual, "page": page, "bbox": bb, "bbox_space": "pdf_pt",
                             "note": f"window {i + 1} of {item}" + (f" (score {sc:.2f})" if sc < 1 else "")}
                            for i, (_, bb, sc) in enumerate(hit)]}
        if w is None:
            rows.append(row)
        else:
            w.clear()
            w.update(row)
        filled += 1
    return filled


def doors_by_room(pdf: str, virtual: str, rows: list[dict], page: int = 0) -> int:
    """Per-room DOORS for rooms the by-name question could not ask (Group Room x10: one name, ten rooms): DR's swing
    arcs (stoa.solvers.openings find_swings / group_doors) and DR's own door-to-room rule (a doorway on an edge of the
    room's traced outline, 0.45 m tolerance) applied to each copy's outline. Local and fast (arcs read once)."""
    try:
        from stoa.evidence import linework as L
        from stoa.solvers import openings as O

        sc = L.sheet_scales(pdf, page)
        sw = O.find_swings(pdf, page, float(sc[0]) if sc else None)
        if not sw.get("arcs") or not sw.get("scale_n"):
            return 0
        doors = O.group_doors(sw["arcs"])
        tol = 0.45 / (O.PT_M * sw["scale_n"])
    except Exception:  # noqa: BLE001
        return 0
    by_key: dict = {}
    for r in rows:
        if r.get("section") == "room":
            by_key.setdefault((r["item"], r.get("copy", 1)), {})[r["quantity"]] = r
    filled = 0
    for (item, copy), d in by_key.items():
        if "doors" in d:  # asked by name already (a reading or an honest abstention)
            continue
        a = d.get("area")
        poly = next((e.get("polygon") for e in (a or {}).get("evidence") or [] if e.get("polygon")), None)
        if not poly or len(poly) < 3:
            continue
        hit = [dr for dr in doors if any(O._on_edge(poly, ((u[0] + v[0]) / 2, (u[1] + v[1]) / 2), tol)
                                         for u, v in O.doorways(dr))]
        ev = []
        for i, dr in enumerate(hit[:MAX_MARKS]):
            cx, cy = dr["center"]
            h = 0.5 / (O.PT_M * sw["scale_n"])
            ev.append({"file": virtual, "page": page, "bbox": [round(cx - h, 1), round(cy - h, 1), round(cx + h, 1),
                                                                round(cy + h, 1)], "bbox_space": "pdf_pt",
                       "note": f"door {i + 1} of {item} ({copy}/{(a or {}).get('copies', 1)}): {dr['kind']} swing"})
        rows.append({"section": "room", "item": item, "copy": copy, "copies": (a or {}).get("copies", 1),
                     "quantity": "doors", "unit": "count", "value": len(hit), "confidence": 0.5, "status": "ok",
                     "leaves": sum(int(dr.get("leaves") or 1) for dr in hit),
                     "method": "openings swing arcs on the room's traced outline", "seconds": 0, "evidence": ev,
                     "assumptions": ["doors = drawn door swings whose doorway lies on the room's traced outline "
                                     "(within 0.45 m); a double door = 1", f"scale 1:{sw['scale_n']:g}"]})
        filled += 1
    return filled


# ------------------------------------------------------------------------------------------------------- the job


_VOID_RX = re.compile(r"^(?:void|open\s+to\s+below|vide|luftraum)\b", re.I)


def area_tags(pdf: str, page: int = 0) -> list[dict]:
    """Rooms whose area is PRINTED in their tag ('NCOIC Office / 2.05 / 81 SF', VP's stoa.sheet.labels), plan views
    only (a reflected ceiling plan repeats the tags), named tags only (a table total is not a room)."""
    try:
        from stoa.sheet.labels import as_area_tags

        tags = as_area_tags(pdf, None, page)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in tags:
        if not t.get("name") or re.search(r"\b(rcp|reflected|ceiling)\b", str(t.get("view") or ""), re.I):
            continue
        name = _clean(t["name"])
        out.append({"name": name, "number": t.get("number"), "key": name.lower(), "value": t["value"],
                    "unit": t["unit"], "text": t.get("text"), "bbox": t["bbox"], "area_box": t.get("tag_bbox"),
                    "view": t.get("view"),
                    "pt": [round((t["bbox"][0] + t["bbox"][2]) / 2, 2), round((t["bbox"][1] + t["bbox"][3]) / 2, 2)]})
    counts: dict[str, int] = {}
    for r in out:
        counts[r["key"]] = counts.get(r["key"], 0) + 1
    seen: dict[str, int] = {}
    for r in out:
        seen[r["key"]] = seen.get(r["key"], 0) + 1
        r["copy"], r["copies"] = seen[r["key"]], counts[r["key"]]
    return out[:MAX_ROOMS]


def printed_table(pdf: str, page: int = 0, picture: str | None = None) -> list[dict]:
    """The sheet's own printed AREA TABLE ('WEST WING DORMITORY = 6,095 SF'; E1's stoa.evidence.areatable): one row
    per printed line, value and unit as printed (exact to the drawing). A picture: the rows of its already-read text
    layer (image_rows makes no model call)."""
    try:
        from stoa.evidence import areatable as AT

        got = AT.image_rows(picture) if picture else AT.rows(pdf, page)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for label, value, unit, bbox in got or ():
        out.append({"label": _clean(label), "value": value, "unit": unit, "bbox": [round(v, 1) for v in bbox]})
    return out


def printed_row(virtual: str, page, r: dict, space: str = "pdf_pt") -> dict:
    return {"section": "printed", "item": r["label"], "quantity": "area", "unit": r["unit"], "value": r["value"],
            "confidence": 0.9, "status": "ok", "method": "printed area table (stoa.evidence.areatable)", "seconds": 0,
            "assumptions": [f"as printed on the sheet: '{r['label']} = {r['value']:,g} {r['unit'].upper()}'"],
            "evidence": [{"file": virtual, "page": page, "bbox": r["bbox"], "bbox_space": space,
                          "note": f"printed '{r['label']}' = {r['value']:,g} {r['unit']}"}]}


def tag_rows(virtual: str, page: int, r: dict) -> dict:
    """The printed area of a tagged room, as printed (no measurement): exact to the drawing."""
    label = f"{r['name']} ({r['number']})" if r.get("number") else r["name"]
    return {"section": "room", "item": label, "copy": r["copy"], "copies": r["copies"], "quantity": "area",
            "unit": r["unit"], "value": r["value"], "confidence": 0.9, "status": "ok",
            "method": "printed room tag (stoa.sheet.labels)", "seconds": 0, "label_box": r["bbox"],
            "assumptions": [f"area as printed in the room's tag: '{r.get('text')}'" +
                            (f" (view: {r['view']})" if r.get("view") else "")],
            "evidence": [{"file": virtual, "page": page, "bbox": r.get("area_box") or r["bbox"],
                          "bbox_space": "pdf_pt", "note": f"printed area '{r.get('text')}' of {label}"}]}


def plan_items(abs_path: str, page: int, picture: bool = False, picture_src: str | None = None) -> dict:
    """What the takeoff will ask: {'kind', 'rooms', 'slabs', 'items': [{section, item, quantity, question, ...}]}.
    A sheet that calls out slab types is a foundation / slab plan: its takeoff = the slab types (its other text is
    notes, not rooms). A sheet with printed room tags: those rooms, their printed areas. Else the room labels."""
    is_pdf = abs_path.lower().endswith(".pdf")
    slabs = slab_types(abs_path, page) if is_pdf else []
    tags = area_tags(abs_path, page) if is_pdf and not slabs else []
    rooms = [] if slabs or tags or not is_pdf else [r for r in room_labels(abs_path, page)
                                                       if not _VOID_RX.search(r["name"])]
    items = []
    for r in tags:
        items.append({"section": "room", "kind": "tag", "item": r["name"], "copy": r["copy"], "copies": r["copies"],
                      "quantity": "area", "tag": r})
        if r["copies"] == 1:
            for q, text, unit in ROOM_QS[1:]:
                items.append({"section": "room", "item": f"{r['name']} ({r['number']})" if r.get("number")
                              else r["name"], "copy": 1, "copies": 1, "quantity": q, "unit": unit,
                              "question": text.format(room=r["name"]), "label_box": r["bbox"]})
    rooms_or_tags = rooms or tags
    for r in rooms:
        if picture or r.get("by_label", r["copies"] > 1):
            # a picture's traced copy: names come from OCR ('Faress Roons' on X1's DfE scan), so asking by name fell
            # to typical-value guesses (0 / 2 X1 golds) while the tracer at each label read 12 / 33 rooms within 2 % of
            # the clean picture (most others within 5 %) in 17 s: measure every room where its label is
            items.append({"section": "room", "item": r["name"], "copy": r["copy"], "copies": r["copies"],
                          "kind": "trace", "room": r, "label_box": r["bbox"]})
            # doors of every traced room (a picture's too): DR's swing arcs on its outline after the loop
            # (doors_by_room). Asked by the OCR'd name, `openings` found another / no room (QA 20:45: 'Fitness Room'
            # doors 0 on Stoa's DfE PNG).
            continue
        for q, text, unit in ROOM_QS:
            if q == "baseboard" and _no_skirting(r["name"]):
                continue
            items.append({"section": "room", "item": r["name"], "copy": 1, "copies": 1, "quantity": q, "unit": unit,
                          "question": text.format(room=r["name"]), "label_box": r["bbox"]})
    table = printed_table(abs_path, page, picture=picture_src) if is_pdf or picture_src else []
    for t in table:
        items.append({"section": "printed", "kind": "printed", "item": t["label"], "quantity": "area", "row": t})
    for s in slabs:
        items.append({"section": "slab", "item": s["text"], "quantity": "slab_area", "unit": "m2",
                      "question": f"What is the area of the {s['text']}?", "label_box": s["bbox"]})
    for q, text, unit in SHEET_QS if not slabs else ():  # a foundation / slab plan draws no doors or windows
        items.append({"section": "sheet", "item": "Whole sheet", "quantity": q, "unit": unit, "question": text})
    if not rooms_or_tags and not slabs and not table:
        items.append({"section": "sheet", "item": "Whole sheet", "quantity": "area", "unit": "m2",
                      "question": "What is the total floor area of this plan?"})
    kind = "vector-pdf" if is_pdf else "image"
    return {"kind": kind, "rooms": rooms_or_tags, "slabs": slabs, "items": items,
            "source": ", ".join(x for x, on in (("slab callouts", slabs), ("printed room tags", tags),
                                               ("room labels", rooms), ("printed area table", table)) if on)}


def _no_skirting(name: str) -> bool:
    try:
        from stoa.evidence import lineplan

        return bool(lineplan._NO_SKIRTING.search(name))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------- the estimator's view (captain 21:13 / 21:14)
# Engineer research (artifacts/RESEARCH/engineer-research.md): estimators trust a takeoff whose areas are coloured by
# room type with a legend, whose totals are NET with the rule shown, whose low-confidence items sit in a review queue
# OUTSIDE the totals, whose door counts give sets AND leaves, and whose rows carry the sheet ID + page. All of it is
# derived at READ time from the stored rows (finish()), so a cached takeoff gets it without a re-run.
ROOM_TYPES = (  # key, legend label, colour, name words (prefix match, first type that matches wins). English plus the
    # dataset's Estonian (Maleva 18), German / Swiss and Finnish room names.
    ("wet", "Toilets & wet rooms", "#3b8fd0",
     r"toilet|wc\b|w\.c|shower|bath|washroom|lavator|hygiene|changing|laundry|sauna|saun\b|cleaner|urinal|en.?suite|"
     r"utility|wash|tualett|vannituba|dušš|pesuruum|pesukoda|koristus|bad\b|dusche|kph\b|kylpy|pesuhuone|pukuhuone|"
     r"siivous"),
    ("circulation", "Circulation", "#9aa3ad",
     r"corridor|circulation|lobby|stair|lift|elevator|landing|entrance|vestibule|foyer|walkway|passage|hallway|"
     r"ramp|porch|hall\s*way|entry\b|airlock|escape|koridor|esik\b|trepikoda|fuajee|tuulekoda|korridor|flur\b|"
     r"diele|treppenhaus|eingang|käytävä|eteinen|porras|tuulikaappi|aula\b"),
    ("living", "Living", "#3fb5a3",
     r"living|lounge|family|day\s*room|common|dayroom|elutuba|wohnen|wohnzimmer|zimmer|olohuone|ühistegevus|"
     r"room\s*\d"),
    ("kitchen", "Kitchen & servery", "#c46bb3",
     r"kitchen|servery|food|pantry|cafe|canteen|dining|break\s*room|tea\b|köök|küche|keittiö|essen"),
    ("teaching", "Teaching & learning", "#e6a92e",
     r"classroom|class\b|group\s*room|teaching|english|maths?\b|science|lab|art\b|music|drama|ict\b|mfl\b|library|"
     r"learning|studio|swr\b|seminar|tutorial|resource|practical|nursery|sen\b|sensory|soft\s*play|quiet\s*room|"
     r"klass|arvutiklass|ringiruum|humanitaar|luokka"),
    ("hall", "Halls & sport", "#e0663f",
     r"hall\b|gym|sports?\b|fitness|auditorium|assembly|multi.?purpose|theatre|saal\b|turnhalle|liikuntasali"),
    ("staff", "Offices & staff", "#5fa84a",
     r"office|staff|admin|meeting|reception|interview|head|reprographic|workroom|senco|medical|sick|therapy|"
     r"parent|mi\s+room|conference|ncoic|nco\b|clinic|consult|copy|print|personal|töötaja|hooldus|valveruum|kabinet|"
     r"büro|toimisto"),
    ("storage", "Storage", "#a07c4f",
     r"store|storage|stock|cupboard|closet|locker|wardrobe|bin\b|refuse|abiruum|panipaik|ladu\b|liikumisvahend|"
     r"reduit|abstell|keller|lager|varasto"),
    ("plant", "Plant & services", "#5d6b7a",
     r"plant\b|plant\s*room|riser|server|electrical|elec\b|mechanical|mech\b|boiler|comms|data\b|switch|meter|duct|"
     r"shaft|tank|sprinkler|generator|ups\b|telecom|janitor|sõlm|soojus|el\.?\s*kilp|kilp\b|ventilatsioon|"
     r"technik|heizung|tekninen|konehuone"),
    ("sleep", "Bedrooms & dorms", "#6f84d6", r"bed|dorm|sleep|magamis|schlaf|makuuhuone"),
)
OTHER_TYPE = ("other", "Other rooms", "#b9b0a3")
_TYPE_RX = [(k, re.compile(r"\b(?:" + w + ")", re.I)) for k, _, _, w in ROOM_TYPES]
TYPE_LABEL = {k: lab for k, lab, _, _ in (*ROOM_TYPES, (*OTHER_TYPE, ""))}
TYPE_COLOR = {k: c for k, _, c, _ in (*ROOM_TYPES, (*OTHER_TYPE, ""))}
REVIEW_BELOW = 0.4  # a reading under this confidence ('rough') is listed for review and kept OUT of every total
SUM_MIN_CONF = REVIEW_BELOW
WET_NOTE = "wet room: usually tiled, skirting usually not fitted (shown in the total; switch wet rooms off to drop it)"
_VOCAB = None


def room_type(name: str) -> str:
    """The room's type from its printed name ('Secondary Classroom' -> teaching, 'Changing Toilet' -> wet, Estonian
    'TUALETTRUUM' -> wet); a name OCR garbled ('Saconcary Classro:m') is matched word by word against the types'
    words (difflib, 0.8, words of 5+ letters: shorter ones matched the wrong type)."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    for k, rx in _TYPE_RX:
        if rx.search(text):
            return k
    global _VOCAB
    if _VOCAB is None:
        import itertools

        _VOCAB = {}
        for k, _, _, w in ROOM_TYPES:
            for word in itertools.chain.from_iterable(re.findall(r"[a-z]{5,}", x) for x in w.split("|")):
                _VOCAB.setdefault(word, k)
    import difflib

    for tok in re.findall(r"[A-Za-z]{5,}", text.lower()):  # OCR-garbled long words only ('Saconcary Classro:m')
        hit = difflib.get_close_matches(tok, list(_VOCAB), n=1, cutoff=0.8)
        if hit:
            return _VOCAB[hit[0]]
    return OTHER_TYPE[0]


def leaves_of(row: dict) -> int | None:
    """Door LEAVES behind a door count: a double door (two leaves on one doorway) = 1 door, 2 leaves. From the row's
    own field (doors_by_room) or from its one-box-per-door evidence; None when the boxes are not all there."""
    if row.get("quantity") != "doors" or not isinstance(row.get("value"), (int, float)):
        return None
    if isinstance(row.get("leaves"), int):
        return row["leaves"]
    if row["value"] == 0:
        return 0
    ev = row.get("evidence") or []
    if len(ev) != int(row["value"]):
        return None
    two = re.compile(r"2 leaves|double door|double swing|\bpair\b|folding pair", re.I)
    return sum(2 if two.search(e.get("note") or "") and "double-acting" not in (e.get("note") or "") else 1
               for e in ev)


def _perimeter(row: dict) -> float | None:
    for a in row.get("assumptions") or []:
        m = re.search(r"perimeter ([\d.]+) m", str(a))
        if m:
            return float(m.group(1))
    return None


def review_reason(row: dict) -> str | None:
    """Why a row needs a person's look before it is priced (and stays out of the totals), or None."""
    sec, q, v = row.get("section"), row.get("quantity"), row.get("value")
    if sec == "total":
        return None
    if v is None or not isinstance(v, (int, float)):
        if sec in ("room", "slab") and q in ("area", "baseboard", "slab_area"):
            why = {"timeout": "no reading in time", "error": "the reader failed"}.get(row.get("status"), "no reading")
            return f"{why}: measure it on the drawing (Measure tool)"
        return None
    if row.get("status") == "estimate":
        return "a typical value for this kind of room, not measured on the drawing"
    c = float(row.get("confidence") or 0)
    if c < REVIEW_BELOW:
        return f"rough reading (confidence {c:.2f})"
    if q == "baseboard":
        per = _perimeter(row)
        if per and v < 0.3 * per:
            return f"only {v / per:.0%} of the {per:.1f} m outline counted as wall: check the openings"
    return None


_ISO_NO = re.compile(r"^[A-Z0-9]{2,8}(?:-[A-Z0-9]{1,8}){4,8}$")  # ISO 19650 / BS 1192 'SEN-DFE-XX-01-D-X-0202'
_ISO_REV = re.compile(r"^[PCAT]\d{2}(?:\.\d+)?$")  # its revision code: P01 preliminary, C01 contractual ...


def sheet_rev(abs_path: str | None, page: int) -> tuple[str | None, str | None]:
    """(sheet number, revision) from the title block. An ISO 19650 drawing number (5+ hyphenated fields) wins and its
    revision code (P01 / C01, printed on a page that says REVISION) is kept as the revision; else the largest sheet
    number (stoa.formats.pages.sheet_id: 'M-101A', 'S-100F'), never a revision code. (None, None) when none."""
    if not abs_path or not str(abs_path).lower().endswith(".pdf"):
        return None, None
    try:
        import pymupdf

        with pymupdf.open(abs_path) as d:
            spans = [sp for b in d[int(page or 0)].get_text("dict").get("blocks", []) for ln in b.get("lines", [])
                     for sp in ln.get("spans", []) if sp.get("text", "").strip()]
    except Exception:  # noqa: BLE001
        return None, None
    texts = [(sp["text"].strip(), float(sp.get("size") or 0)) for sp in spans]
    iso = sorted((t for t in texts if _ISO_NO.match(t[0]) and re.search(r"\d", t[0])), key=lambda t: -t[1])
    rev = None
    if any(re.search(r"\brev(?:ision)?\b", t, re.I) for t, _ in texts):
        revs = sorted((t for t in texts if _ISO_REV.match(t[0])), key=lambda t: -t[1])
        rev = revs[0][0] if revs else None
    if iso:
        return iso[0][0], rev
    try:
        from stoa.formats.pages import sheet_id

        sid = sheet_id(abs_path, int(page or 0))
    except Exception:  # noqa: BLE001
        sid = None
    if sid and _ISO_REV.match(sid):
        sid = None
    return sid, rev


def sheet_of(abs_path: str | None, page: int) -> str | None:
    return sheet_rev(abs_path, page)[0]


def _sum_row(item: str, quantity: str, unit: str, rows: list[dict], rule: str, flags: list[str] | None = None) -> dict:
    return {"section": "total", "item": item, "quantity": quantity, "unit": unit,
            "value": round(sum(float(r["value"]) for r in rows), 2),
            "confidence": round(min(float(r.get("confidence") or 0) for r in rows), 3), "status": "ok",
            "method": "sum of the rows above (review rows left out)", "assumptions": [rule, *(flags or [])],
            "evidence": [], "seconds": 0, "of": [r["id"] for r in rows if r.get("id")]}


def totals(rows: list[dict]) -> list[dict]:
    """Derived rows from the finished rows: rooms labelled, NET floor area, skirting total (with and without wet
    rooms), door sets / leaves. Every sum leaves the review rows out and says how many it used."""
    out = []
    area = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "area"]
    if area:
        out.append({"section": "total", "item": "Rooms labelled", "quantity": "rooms", "unit": "count",
                    "value": len(area), "confidence": 0.8, "status": "ok", "method": "room labels on the sheet",
                    "assumptions": [f"{len({r.get('item') for r in area})} different names; a name printed twice = "
                                    "two rooms"], "evidence": [], "seconds": 0})
    for unit in ("m2", "ft2"):
        ok = [r for r in area if (r.get("unit") or "m2") == unit and isinstance(r.get("value"), (int, float))
              and not r.get("review")]
        if ok:
            n_all = sum(1 for r in area if (r.get("unit") or "m2") == unit or r.get("value") is None)
            out.append(_sum_row("Net floor area (sum of rooms)", "area_sum", unit, ok,
                                f"NET: the floor inside each room's wall faces; {len(ok)} of {n_all} rooms, "
                                f"{n_all - len(ok)} in Review are left out"))
    base = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "baseboard"
            and isinstance(r.get("value"), (int, float)) and not r.get("review")]
    if base:
        wet = [r for r in base if r.get("wet")]
        rule = "NET: each room's wall-face perimeter minus its door openings (stairs, lifts, risers, plant: none)"
        out.append(_sum_row("Skirting total", "baseboard_sum", "m", base, rule,
                            [f"includes {len(wet)} wet rooms ({sum(r['value'] for r in wet):.1f} m): " + WET_NOTE]
                            if wet else []))
        dry = [r for r in base if not r.get("wet")]
        if wet and dry:
            out.append(_sum_row("Skirting total without wet rooms", "baseboard_dry", "m", dry,
                                rule + "; wet rooms (toilets, showers, changing, laundry: usually tiled) left out"))
    sd = next((r for r in rows if r.get("section") == "sheet" and r.get("quantity") == "doors"
               and isinstance(r.get("value"), (int, float))), None)
    if sd is not None and sd.get("leaves") is not None:
        out.append({"section": "total", "item": "Door leaves (whole sheet)", "quantity": "door_leaves",
                    "unit": "count", "value": sd["leaves"], "confidence": sd.get("confidence"), "status": "ok",
                    "method": sd.get("method", ""), "evidence": [], "seconds": 0, "of": [sd.get("id")],
                    "assumptions": [f"{sd['value']} door sets ({sd['leaves'] - sd['value']} of them double: 2 "
                                    "leaves each)"]})
    return out


def finish(st: dict, virtual: str | None = None, abs_path: str | None = None) -> dict:
    """A stored takeoff -> the table people read: per row an id, the sheet ID + page, the room type (+ wet flag), door
    leaves, the review reason; then the totals recomputed from the rows outside the review queue. A job started from
    another path to the same file (catalogue warm-up vs the visitor's upload) gets the visitor's path in its evidence."""
    st = dict(st)
    rows = [dict(r) for r in st.get("rows") or [] if r.get("section") != "total"]
    old = st.get("file")
    if virtual and old and virtual != old:
        for r in rows:
            r["evidence"] = [{**e, "file": virtual} if e.get("file") in (old, None) else e
                             for e in r.get("evidence") or []]
        st["file"] = virtual
    if "sheet" not in st:
        st["sheet"], st["revision"] = sheet_rev(abs_path or st.get("abs"), st.get("page") or 0)
    for i, r in enumerate(rows):
        r["id"] = f"r{i}"
        ev = r.get("evidence") or []
        r["sheet"], r["revision"] = st.get("sheet"), st.get("revision")
        r["page"] = next((e.get("page") for e in ev if e.get("page") is not None), st.get("page"))
        if r.get("section") in ("room", "printed"):
            r["room_type"] = room_type(r.get("item", ""))
            r["wet"] = r["room_type"] == "wet"
        if r.get("quantity") == "doors":
            r["leaves"] = leaves_of(r)
        r["review"] = review_reason(r)
        flags = []
        if r.get("quantity") == "baseboard" and r.get("wet"):
            flags.append(WET_NOTE)
        r["flags"] = flags
    # cross-check of two readings of one room: no door swing on its outline, but the skirting rule found openings in
    # its walls (QA 20:45 'Fitness Room' 0 on the DfE PNG: the vector sheet has 1; its arc was lost in the tracing)
    gaps = {}
    for r in rows:
        if r.get("quantity") == "baseboard":
            m = next((re.search(r"minus (\d+) opening", str(a)) for a in r.get("assumptions") or []
                      if re.search(r"minus (\d+) opening", str(a))), None)
            if m:
                gaps[(r.get("item"), r.get("copy", 1))] = int(m.group(1))
    for r in rows:
        if r.get("quantity") == "doors" and r.get("section") == "room" and r.get("value") == 0 and not r.get("review"):
            n = gaps.get((r.get("item"), r.get("copy", 1)), 0)
            if n:
                r["review"] = (f"no door swing found on its outline, but its walls have {n} opening"
                               f"{'s' if n > 1 else ''}: a door may be drawn without an arc")
    rows.extend(totals(rows))
    st["rows"] = rows
    st["review_count"] = sum(1 for r in rows if r.get("review"))
    return st


def _union(boxes: list) -> list | None:
    boxes = [b for b in boxes if b and len(b) == 4]
    if not boxes:
        return None
    return [round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1), round(max(b[2] for b in boxes), 1),
            round(max(b[3] for b in boxes), 1)]


def _shape(r: dict) -> dict | None:
    """The row's outline (polygon) or box for the plan overlay."""
    for e in r.get("evidence") or []:
        if e.get("polygon") or e.get("bbox"):
            return {"id": r["id"], "item": r.get("item"), "copy": r.get("copy", 1), "value": r.get("value"),
                    "unit": r.get("unit"), "review": bool(r.get("review")), "bbox": e.get("bbox"),
                    "polygon": e.get("polygon"), "space": e.get("bbox_space") or "pdf_pt", "page": e.get("page")}
    return None


def _scale_text(st: dict) -> dict:
    n = st.get("scale_n")
    out = {"n": n, "text": f"1:{n:g}" if isinstance(n, (int, float)) and n else None}
    if st.get("traced"):
        out["source"] = f"picture traced into lines: {st['traced']}"
    elif n:
        out["source"] = "printed on the sheet (title block / scale bar), checked against the door swings"
    else:
        out["source"] = "no scale found: areas and lengths are not measured"
    return out


def summary(st: dict) -> dict:
    """'This floor at a glance' (W5's card) from a FINISHED takeoff (finish()): scale + source, net area, rooms by
    type (count, m², outlines), doors (sets / leaves), windows, skirting (with / without wet rooms), review queue.
    Every number carries `ids` (rows of GET /status) and `focus` (a box in the drawing's space) to click to."""
    rows = st.get("rows") or []
    by_id = {r["id"]: r for r in rows if r.get("id")}
    space = next((e.get("bbox_space") for r in rows for e in r.get("evidence") or [] if e.get("bbox_space")),
                 "image_px" if str(st.get("kind", "")).startswith("image") else "pdf_pt")

    def tot(q, unit=None):
        return next((r for r in rows if r.get("section") == "total" and r.get("quantity") == q
                     and (unit is None or r.get("unit") == unit)), None)

    def focus(ids):
        return _union([e.get("bbox") for i in ids for e in (by_id.get(i, {}).get("evidence") or [])[:1]])

    def num(r):
        if r is None:
            return None
        return {"value": r.get("value"), "unit": r.get("unit"), "confidence": r.get("confidence"),
                "how_sure": how_sure(r.get("confidence")), "ids": r.get("of") or [r.get("id")],
                "focus": focus(r.get("of") or [r.get("id")]), "rule": " ".join(r.get("assumptions") or []),
                "review": r.get("review"), "estimate": r.get("status") == "estimate"}

    area_rows = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "area"]
    unit = "ft2" if sum(r.get("unit") == "ft2" for r in area_rows) > len(area_rows) / 2 else "m2"
    types = []
    for k, label, color, _ in (*ROOM_TYPES, (*OTHER_TYPE, "")):
        rs = [r for r in area_rows if r.get("room_type") == k]
        if not rs:
            continue
        ok = [r for r in rs if isinstance(r.get("value"), (int, float)) and not r.get("review")
              and (r.get("unit") or "m2") == unit]
        types.append({"type": k, "label": label, "color": color, "rooms": len(rs), "measured": len(ok),
                      "review": sum(1 for r in rs if r.get("review")),
                      "area": round(sum(r["value"] for r in ok), 2) if ok else None, "unit": unit,
                      "names": sorted({r.get("item") for r in rs})[:12], "ids": [r["id"] for r in rs],
                      "focus": focus([r["id"] for r in rs]), "outlines": [s for s in map(_shape, rs) if s]})
    sd = next((r for r in rows if r.get("section") == "sheet" and r.get("quantity") == "doors"), None)
    sw = next((r for r in rows if r.get("section") == "sheet" and r.get("quantity") == "windows"), None)
    doors = None
    if sd is not None:
        doors = {**(num(sd) or {}), "sets": sd.get("value"), "leaves": sd.get("leaves"),
                 "double": (sd["leaves"] - sd["value"]) if isinstance(sd.get("leaves"), int)
                 and isinstance(sd.get("value"), (int, float)) else None,
                 "marks": [e.get("bbox") for e in sd.get("evidence") or [] if e.get("bbox")],
                 "rule": "a door = one doorway; a double door = 1 set, 2 leaves; counted from the drawn door "
                         "symbols", "missing": sd.get("missing") or []}
    windows = None
    if sw is not None:
        windows = {**(num(sw) or {}), "marks": [e.get("bbox") for e in sw.get("evidence") or [] if e.get("bbox")]}
    base_all = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "baseboard"]
    skirting = None
    if tot("baseboard_sum"):
        skirting = {**num(tot("baseboard_sum")), "without_wet": (tot("baseboard_dry") or {}).get("value"),
                    "wet_rooms": sum(1 for r in base_all if r.get("wet") and not r.get("review")),
                    "rooms": len(base_all), "review": sum(1 for r in base_all if r.get("review")),
                    "wet_note": WET_NOTE}
    review = [{"id": r["id"], "item": r.get("item"), "copy": r.get("copy"), "copies": r.get("copies"),
               "quantity": QUANTITY_LABEL.get(r.get("quantity", ""), r.get("quantity")), "value": r.get("value"),
               "unit": r.get("unit"), "confidence": r.get("confidence"), "reason": r["review"],
               "focus": (r.get("evidence") or [{}])[0].get("bbox") or r.get("label_box")}
              for r in rows if r.get("review")]
    area = num(tot("area_sum", unit))
    if area is None:  # no rooms: the sheet's own total (a printed 'TOTAL SECOND FLOOR' row, the plan's floor area)
        sa = next((r for r in rows if r.get("section") == "sheet" and r.get("quantity") == "area"
                   and isinstance(r.get("value"), (int, float)) and not r.get("review")), None)
        tp = next((r for r in rows if r.get("section") == "printed" and re.search(r"\btotal\b", str(r.get("item")), re.I)
                   and re.search(r"floor|gross|building|overall", str(r.get("item")), re.I)), None)
        pick = sa or tp
        if pick is not None:
            why = next((a for a in pick.get("assumptions") or [] if "printed" in a), None) or pick.get("item")
            area = {**num(pick), "rule": f"the sheet's own total, not a sum of rooms: {why}"[:300]}
            unit = pick.get("unit") or unit
    return {"status": "done", "job": st.get("job"), "file": st.get("file"), "page": st.get("page"),
            "sheet": st.get("sheet"), "revision": st.get("revision"), "kind": st.get("kind"),
            "source": st.get("source"), "space": space,
            "scale": _scale_text(st), "unit": unit,
            "area": area, "rooms": {"labelled": len(area_rows),
                                                          "measured": sum(t["measured"] for t in types),
                                                          "review": sum(t["review"] for t in types)},
            "by_type": types, "doors": doors, "windows": windows, "skirting": skirting,
            "slabs": [{"item": r.get("item"), **num(r)} for r in rows if r.get("section") == "slab"],
            "printed": [{"item": r.get("item"), **num(r)} for r in rows if r.get("section") == "printed"],
            "review": review, "seconds": st.get("seconds"), "finished": st.get("finished"),
            "cost": st.get("cost") or {"tier": "cheap", "api_calls": 0}, "engine": st.get("engine")}


IMAGE_EXT = (".png", ".jpg", ".jpeg")
# the detector's whole-sheet pass on X1's DfE scan took 123 s for '1 window' (clean PDF: 79): cap it on pictures
PICTURE_SHEET_BUDGET_S = 90.0
TRACED_CAP = 0.5  # = stoa.solvers.upload.VECTOR_CAP: lines traced from a picture


def traced(img: str) -> tuple[str, float, str] | None:
    """A picture of a CAD sheet -> P1's traced vector copy (stoa.formats.vectorize: the image's lines as PDF strokes,
    its OCR text as invisible text, cached next to the upload) + the factor pdf_pt -> image px + how the paper size
    was found. The same step the upload solver takes when its image readers find nothing (Upload._as_vector)."""
    from stoa.formats import vectorize as V

    hit, how = V.paper(img), "from its DPI metadata"
    if hit is None:
        from stoa.formats.paper import paper_of_image
        from stoa.solvers.raster import textlayer as TL

        hit = paper_of_image(img, [i.text for i in TL.text_layer(img).items])
        how = "from its pixel size and the sheet size it prints"
    if hit is None:
        return None
    dpi, sheet = hit
    return str(V.traced_copy(img, dpi)), dpi / 72.0, f"{sheet} at {dpi:.0f} dpi {how}"


def to_image(rows: list[dict], virtual: str, f: float, note: str) -> None:
    """Evidence read on the traced copy -> the uploaded picture's pixels; confidence capped like the upload solver."""
    for r in rows:
        if r.pop("_px", False):
            continue
        for e in r.get("evidence") or []:
            e["file"], e["page"], e["bbox_space"] = virtual, None, "image_px"
            if e.get("bbox"):
                e["bbox"] = [round(v * f, 1) for v in e["bbox"]]
            for k in ("polygon", "polyline"):
                if e.get(k):
                    e[k] = [[round(x * f, 1), round(y * f, 1)] for x, y in e[k]]
        if r.get("label_box"):
            r["label_box"] = [round(v * f, 1) for v in r["label_box"]]
        if isinstance(r.get("confidence"), (int, float)) and r["confidence"] > TRACED_CAP:
            r["confidence"] = TRACED_CAP
        if r.get("value") is not None and r.get("section") != "total":
            r["assumptions"] = [*(r.get("assumptions") or []), f"read on the picture's lines traced into a vector "
                                                              f"drawing ({note}); confidence capped at {TRACED_CAP}"]


def run(abs_path: str, virtual: str, page: int = 0, out_dir: str | Path | None = None, log=print) -> dict:
    """The whole takeoff of one sheet; progress.json / result.json in out_dir (when given) after every row.
    A PNG / JPEG is read through its traced vector copy (see traced()), evidence mapped back to its pixels."""
    t0 = time.time()
    out_dir = Path(out_dir) if out_dir else None
    image, f, how = None, 1.0, ""
    if abs_path.lower().endswith(IMAGE_EXT):
        if out_dir:
            _write_json(out_dir / "progress.json", {"status": "running", "file": virtual, "page": page, "done": 0,
                                                    "total": 0, "current": "tracing the picture's lines (once per file)"})
        tr = traced(abs_path)
        if tr is not None:
            image, (abs_path, f, how) = abs_path, tr
            page = 0
            log(f"[takeoff] picture traced: {abs_path} ({how})")
    raw_img = image is None and abs_path.lower().endswith(IMAGE_EXT)  # a picture we could not trace (no paper size)
    plan = plan_items(abs_path, page, picture=image is not None, picture_src=image or (abs_path if raw_img else None))
    items = plan["items"]
    n = _scale(abs_path, page) if plan["kind"] == "vector-pdf" else None
    rows: list[dict] = []
    state = {"status": "running", "file": virtual, "page": page, "engine": ENGINE_V, "kind": plan["kind"],
             "started": t0, "done": 0, "total": len(items), "current": "", "rooms": len(plan["rooms"]),
             "slabs": [s["text"] for s in plan["slabs"]], "source": plan.get("source"), "scale_n": n,
             "rows": rows}
    state["sheet"], state["revision"] = sheet_rev(abs_path, page)
    for i, it in enumerate(items):
        state["current"] = f"{it['item']}: {QUANTITY_LABEL.get(it.get('quantity', 'area'), '')}".strip(": ")
        if out_dir:
            _write_json(out_dir / "progress.json", state)
        if it.get("kind") == "trace":
            rows.extend(measure_copy(abs_path, virtual, page, it["room"], n))
        elif it.get("kind") == "tag":
            rows.append(tag_rows(virtual, page, it["tag"]))
        elif it.get("kind") == "printed":  # a picture's table rows are already in its pixels: mark them so
            px = bool(image or raw_img)
            rows.append(printed_row(virtual, None if px else page, it["row"], "image_px" if px else "pdf_pt"))
            if image:
                rows[-1]["_px"] = True
        else:
            base = {k: it[k] for k in ("section", "item", "quantity", "unit", "question", "copy", "copies",
                                       "label_box") if k in it}
            q = it.get("quantity", "area")
            solver, det, budget = ROUTE.get(f"sheet_{q}" if it["section"] == "sheet" else q, (None, False, 60.0))
            if image and it["section"] == "sheet":
                budget = min(budget, PICTURE_SHEET_BUDGET_S)
            out = ask(abs_path, virtual, it["question"], page, budget_s=budget, solver=solver, detector=det)
            rows.append(row_from_answer(base, out))
            log(f"[takeoff] {i + 1}/{len(items)} {it['question']} -> {out.get('value')!r} {out.get('unit') or ''} "
                f"({out.get('status')}, {out.get('seconds')} s)")
        state["done"] = i + 1
    if plan["rooms"]:
        k = doors_by_room(abs_path, virtual, rows, page)
        log(f"[takeoff] doors per room from the swing arcs on each traced outline: {k} rooms")
    if not image and plan["rooms"]:
        k = windows_by_room(abs_path, virtual, rows, page=page)
        log(f"[takeoff] windows per room from the linework (or the sheet's detector pass): {k} rooms")
    if image:
        to_image(rows, virtual, f, how)
        state.update(kind="image (traced)", traced=how)
    state.update(status="done", current="", finished=time.time(), seconds=round(time.time() - t0, 1),
                 cost={"api_calls": sum(int((r.get("cost") or {}).get("api_calls") or 0) for r in rows),
                       "llm_tokens_in": sum(int((r.get("cost") or {}).get("llm_tokens_in") or 0) for r in rows),
                       # the model URLs point at a closed port here (no_models), so a call that reports tokens was a
                       # disk-cache replay of an earlier local answer (stoa.llm bills cache hits as computed)
                       "llm_calls_replayed": sum(int((r.get("cost") or {}).get("llm_calls") or 0) for r in rows),
                       "tier": "cheap"})
    if out_dir:
        _write_json(out_dir / "result.json", state)
        _write_json(out_dir / "progress.json", {k: v for k, v in state.items() if k != "rows"})
    return state


# ------------------------------------------------------------------------------------------------------ exports
CSV_COLS = ("sheet", "page", "section", "item", "copy", "room_type", "quantity", "value", "unit", "confidence",
            "how_sure", "review", "flags", "leaves", "method", "status", "evidence_box", "evidence_note",
            "assumptions", "question", "file", "revision")


def how_sure(c) -> str:
    """The answer card's words (web/src/customer.ts confidenceWords); under 0.4 = a guess = the Review list."""
    c = float(c or 0)
    return ("very sure" if c >= 0.9 else "fairly sure" if c >= 0.7 else "best estimate" if c >= 0.4 else
            "our guess" if c >= 0.25 else "rough guess" if c > 0 else "no reading")


def flat(row: dict, file: str = "") -> dict:
    ev = (row.get("evidence") or [{}])[0] if row.get("evidence") else {}
    return {"section": row.get("section", ""), "item": row.get("item", ""),
            "copy": f"{row.get('copy')}/{row.get('copies')}" if (row.get("copies") or 1) > 1 else "",
            "quantity": QUANTITY_LABEL.get(row.get("quantity", ""), row.get("quantity", "")),
            "value": "" if row.get("value") is None else row.get("value"), "unit": row.get("unit") or "",
            "confidence": row.get("confidence", ""), "how_sure": how_sure(row.get("confidence")),
            "method": row.get("method", ""), "status": row.get("status", ""),
            "page": (ev.get("page") if ev.get("page") is not None else row.get("page", "")),
            "evidence_box": " ".join(f"{v:.0f}" for v in ev.get("bbox") or []) + (f" {ev.get('bbox_space')}"
                                                                                  if ev.get("bbox") else ""),
            "evidence_note": ev.get("note", "") or row.get("note", ""),
            "assumptions": " | ".join(row.get("assumptions") or []), "question": row.get("question", ""),
            "file": ev.get("file") or file, "sheet": row.get("sheet") or "",
            "room_type": TYPE_LABEL.get(row.get("room_type"), "") if row.get("room_type") else "",
            "review": row.get("review") or "", "flags": " | ".join(row.get("flags") or []),
            "leaves": "" if row.get("leaves") is None else row.get("leaves"), "revision": row.get("revision") or ""}


def to_csv(rows: list[dict], file: str = "") -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS)
    w.writeheader()
    for r in rows:
        w.writerow(flat(r, file))
    return buf.getvalue()


def wide(rows: list[dict]) -> tuple[list[str], list[list]]:
    """One line per room (area, baseboard, doors, windows), then slabs, printed rows, sheet totals and the NET totals:
    the table people read. Each line carries its sheet + page; a line in Review is marked and is not in the totals."""
    head = ["Sheet", "Page", "Item", "Copy", "Room type", "Floor area (m² unless marked)", "Area sure?",
            "Baseboard (m)", "Baseboard sure?", "Doors", "Door leaves", "Windows", "Review (not in totals)", "Method"]
    lines: dict[tuple, dict] = {}
    order = []
    for r in rows:
        if r.get("section") != "room":
            continue
        k = (r.get("item"), r.get("copy", 1))
        if k not in lines:
            lines[k] = {}
            order.append(k)
        lines[k][r.get("quantity")] = r
    out = []
    pg = lambda r: "" if r.get("page") is None else int(r["page"]) + 1  # noqa: E731 - pages as people number them
    sh = lambda r: " rev ".join(x for x in (r.get("sheet"), r.get("revision")) if x)  # noqa: E731
    for k in order:
        d = lines[k]
        g = lambda q: d.get(q, {}).get("value") if d.get(q, {}).get("value") is not None else ""  # noqa: E731
        any_r = next(iter(d.values()))
        copies = any_r.get("copies", 1)
        rev = "; ".join(f"{QUANTITY_LABEL.get(q, q)}: {r['review']}" for q, r in d.items() if r.get("review"))
        out.append([sh(any_r), pg(any_r), k[0], f"{k[1]}/{copies}" if copies > 1 else "",
                    TYPE_LABEL.get(any_r.get("room_type"), ""), g("area"),
                    how_sure(d.get("area", {}).get("confidence")) if "area" in d else "", g("baseboard"),
                    how_sure(d.get("baseboard", {}).get("confidence")) if "baseboard" in d else "", g("doors"),
                    "" if d.get("doors", {}).get("leaves") is None else d["doors"]["leaves"], g("windows"), rev,
                    d.get("area", {}).get("method", "")])
    for r in rows:
        if r.get("section") in ("printed", "slab", "sheet", "total"):
            ft = " [ft²]" if r.get("unit") == "ft2" else ""
            what = "printed" if r.get("section") == "printed" else \
                QUANTITY_LABEL.get(r.get("quantity", ""), r.get("quantity", ""))
            q = r.get("quantity")
            is_area = r.get("unit") in ("m2", "ft2")
            out.append([sh(r), pg(r), f"{r.get('item')}: {what}{ft}" if r.get("section") != "total"
                        else r.get("item"), "", "", r.get("value") if is_area and r.get("value") is not None else "",
                        how_sure(r.get("confidence")) if is_area else "",
                        r.get("value") if q in ("baseboard_sum", "baseboard_dry") else "", "",
                        r.get("value") if q in ("doors", "rooms") else "",
                        r.get("leaves") if q == "doors" and r.get("leaves") is not None else
                        (r.get("value") if q == "door_leaves" else ""),
                        r.get("value") if q == "windows" else "", r.get("review") or "",
                        r.get("method", "") if r.get("section") != "total" else " ".join(r.get("assumptions") or [])])
    return head, out


def _cell(ref: str, v, bold: bool = False) -> str:
    s = ' s="1"' if bold else ""
    if isinstance(v, bool) or v is None or v == "":
        return f'<c r="{ref}"{s}/>' if bold else ""
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"{s}><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{s}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_xml(head: list, body: list[list]) -> str:
    rows = [f'<row r="1">{"".join(_cell(f"{_col(j)}1", h, True) for j, h in enumerate(head))}</row>']
    for i, line in enumerate(body, start=2):
        rows.append(f'<row r="{i}">{"".join(_cell(f"{_col(j)}{i}", v) for j, v in enumerate(line))}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
            'state="frozen"/></sheetView></sheetViews>'
            f'<sheetData>{"".join(rows)}</sheetData></worksheet>')


def to_xlsx(rows: list[dict], file: str = "", title: str = "Takeoff") -> bytes:
    """Two sheets: 'Takeoff' (one line per room) and 'Details' (every reading with its evidence and assumptions).
    openpyxl when installed; else a minimal SpreadsheetML zip written with the standard library (same content)."""
    head, body = wide(rows)
    dhead = list(CSV_COLS)
    dbody = [[flat(r, file)[c] for c in CSV_COLS] for r in rows]
    try:
        import openpyxl  # type: ignore
        from openpyxl.styles import Font  # type: ignore

        wb = openpyxl.Workbook()
        for ws, (h, b) in ((wb.active, (head, body)), (None, (dhead, dbody))):
            ws = ws or wb.create_sheet("Details")
            if ws is wb.active:
                ws.title = "Takeoff"
            ws.append(h)
            for c in ws[1]:
                c.font = Font(bold=True)
            for line in b:
                ws.append([None if v == "" else v for v in line])
            ws.freeze_panes = "A2"
        bio = io.BytesIO()
        wb.save(bio)
        return bio.getvalue()
    except ImportError:
        pass
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.styles+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   f'<sheet name="{escape(title[:31])}" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Details" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'worksheet" Target="worksheets/sheet2.xml"/>'
                   '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                   '<fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '</styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(head, body))
        z.writestr("xl/worksheets/sheet2.xml", _sheet_xml(dhead, dbody))
    return bio.getvalue()


# ------------------------------------------------------------------------------------------------- job runner


def enabled() -> bool:
    """On when STOA_FEATURES lists 'takeoff' or 'all' (the registry's own rule, QA 21:19)."""
    from stoa.features import enabled as on

    return "takeoff" in on()


def job_dir(key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}-p\d+-[a-z0-9]+", key or ""):
        raise ValueError("bad job id")
    return cache_dir() / key


def status(key: str, virtual: str | None = None) -> dict:
    """The job's state; a finished one through finish() (ids, sheet, room types, review, totals)."""
    d = job_dir(key)
    for name in ("result.json", "progress.json", "queued.json"):
        p = d / name
        if p.exists():
            try:
                st = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            st["job"] = key
            if st.get("rows") is not None:
                st = finish(st, virtual)
            if name == "progress.json" and st.get("status") == "running":
                pid = _pid(d)
                if pid and not _alive(pid):
                    st["status"] = "failed"
                    st["error"] = "the takeoff process stopped: " + _tail(d / "log.txt")
            return st
    return {"job": key, "status": "unknown"}


_SUMMARY: dict = {}


def sheet_summary(path: str, page: int = 0, start_if_none: bool = False) -> dict:
    """summary() of the sheet's finished takeoff, memoised by (job, result.json mtime, path); else its progress."""
    abs_path, virtual = resolve(path)
    key = job_key(abs_path, page)
    d = cache_dir() / key
    res = d / "result.json"
    if res.exists():
        mk = (key, res.stat().st_mtime_ns, virtual)
        if mk not in _SUMMARY:
            st = status(key, virtual)
            if st.get("status") != "done":
                return {"status": st.get("status"), "job": key, "file": virtual, "page": page}
            if len(_SUMMARY) > 64:
                _SUMMARY.clear()
            _SUMMARY[mk] = summary(st)
        return _SUMMARY[mk]
    st = status(key, virtual) if d.exists() else {"status": "none", "job": key}
    if start_if_none and st.get("status") in ("none", "unknown", "failed"):
        st = start(path, page)
    return {"status": "none" if st.get("status") == "unknown" else st.get("status"), "job": key, "file": virtual,
            "page": page, "done": st.get("done", 0), "total": st.get("total", 0), "current": st.get("current", ""),
            "error": st.get("error")}


def _pid(d: Path) -> int | None:
    try:
        return int((d / "pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a finished child that nobody waited for is a zombie: not running
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    except (OSError, IndexError):
        return True


def _tail(p: Path, n: int = 3) -> str:
    try:
        return " / ".join(p.read_text(errors="replace").strip().splitlines()[-n:])[-400:]
    except OSError:
        return ""


def start(path: str, page: int = 0, refresh: bool = False) -> dict:
    """Start (or find) the takeoff of one sheet page; returns its status. One niced process per sheet; jobs queue on
    a machine-wide flock (CPU budget, COMMON.md)."""
    abs_path, virtual = resolve(path)
    key = job_key(abs_path, page)
    d = cache_dir() / key
    st = status(key, virtual) if d.exists() else {"status": "unknown"}
    if st.get("status") in ("done", "running", "queued") and not refresh:
        if st.get("status") != "queued" or (_pid(d) and _alive(_pid(d))):
            return st
    d.mkdir(parents=True, exist_ok=True)
    for name in ("result.json", "progress.json"):
        (d / name).unlink(missing_ok=True)
    _write_json(d / "queued.json", {"status": "queued", "file": virtual, "page": page, "done": 0, "total": 0})
    env = {**os.environ, "STOA_PRIVATE": "1", "STOA_DET_TIMEOUT": os.environ.get("STOA_DET_TIMEOUT", "30"),
           "STOA_DR_DET_TIMEOUT": os.environ.get("STOA_DR_DET_TIMEOUT", "30"), "PYTHONUNBUFFERED": "1"}
    cmd = [sys.executable, "-m", "stoa.features.takeoff", "run", abs_path, "--page", str(int(page)), "--virtual",
           virtual, "--out", str(d)]
    lock = cache_dir() / "takeoff.lock"
    full = ["flock", str(lock), "nice", "-n", "15", *cmd]
    with open(d / "log.txt", "ab") as log:
        p = subprocess.Popen(full, cwd=str(REPO), env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    (d / "pid").write_text(str(p.pid))
    threading.Thread(target=p.wait, daemon=True).start()  # reap it (no zombie for _alive)
    st = status(key, virtual)
    st["job"] = key
    return st


# -------------------------------------------------------------------------------------------------------- router
try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import Response
    from pydantic import BaseModel

    router = APIRouter(prefix="/feat/takeoff", tags=["takeoff"])

    class StartReq(BaseModel):
        path: str
        page: int = 0
        refresh: bool = False

    class ExportReq(BaseModel):
        rows: list[dict]
        fmt: str = "csv"
        title: str = "Takeoff"
        file: str = ""

    def _guard() -> None:
        if not enabled():
            raise HTTPException(404, "takeoff feature is off (STOA_FEATURES)")

    def _file(rows: list[dict], fmt: str, file: str, title: str = "Takeoff"):
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(file).stem or "takeoff")[:60]
        if fmt == "xlsx":
            return Response(to_xlsx(rows, file, title), media_type="application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet", headers={"Content-Disposition":
                                                            f'attachment; filename="{stem}-takeoff.xlsx"'})
        if fmt == "csv":
            return Response(to_csv(rows, file), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{stem}-takeoff.csv"'})
        raise HTTPException(400, "fmt must be csv or xlsx")

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "enabled": enabled(), "engine": ENGINE_V}

    @router.post("/start")
    def start_ep(req: StartReq) -> dict:
        _guard()
        try:
            return start(req.path, req.page, req.refresh)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def _virtual(path: str) -> str | None:
        if not path:
            return None
        try:
            return resolve(path)[1]
        except (ValueError, FileNotFoundError):
            return None

    @router.get("/status")
    def status_ep(job: str = Query(...), path: str = "") -> dict:
        _guard()
        try:
            return status(job, _virtual(path))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/summary")
    def summary_ep(path: str = Query(...), page: int = 0, start: int = 0) -> dict:
        """'This floor at a glance' for one sheet page (W5's card): the summary of its finished takeoff (cached:
        result.json + a hash of the file), else {status: none | queued | running, done, total}; start=1 starts the
        takeoff when there is none (the card can offer 'Generate takeoff')."""
        _guard()
        try:
            return sheet_summary(path, page, bool(start))
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/export")
    def export_ep(job: str = Query(...), fmt: str = "csv", path: str = ""):
        _guard()
        try:
            st = status(job, _virtual(path))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if st.get("status") not in ("done", "running"):
            raise HTTPException(409, f"takeoff is {st.get('status')}")
        return _file(st.get("rows") or [], fmt, st.get("file") or "")

    @router.post("/export")
    def export_rows(req: ExportReq):
        _guard()
        return _file(req.rows, req.fmt, req.file, req.title)
except ImportError:  # the CLI works without FastAPI
    router = None


# ----------------------------------------------------------------------------------------------------------- CLI


def no_models() -> None:
    """The takeoff process makes NO model calls (cheap tier by construction, drawings stay here): hosted backends are
    refused (STOA_PRIVATE=1, P1/G1's hard guard) and the local vLLM URLs point at a closed port, so a room no
    deterministic reader can measure says 'no reading' at once instead of a 30-75 s VLM guess per room that also
    loads the GPU server the demo shares. STOA_TAKEOFF_LLM=1 keeps the local models. Before stoa.llm is imported."""
    os.environ["STOA_PRIVATE"] = "1"
    if os.environ.get("STOA_TAKEOFF_LLM", "0") != "1":
        for k in ("LLM_BASE_URL", "LLM2_BASE_URL"):
            os.environ[k] = DEAD_DETECTOR + "/v1"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m stoa.features.takeoff")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("file")
    r.add_argument("--page", type=int, default=0)
    r.add_argument("--virtual", default="")
    r.add_argument("--out", default="")
    e = sub.add_parser("export")
    e.add_argument("result")
    e.add_argument("--fmt", default="csv", choices=("csv", "xlsx"))
    e.add_argument("--out", default="")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        no_models()
        os.chdir(REPO)
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        out = Path(a.out) if a.out else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        try:
            st = run(str(Path(a.file).resolve()), a.virtual or str(Path(a.file).resolve()), a.page, out,
                     log=lambda s: print(s, file=sys.stderr, flush=True))
        except Exception as ex:  # noqa: BLE001 — the UI shows 'failed' with the reason
            import traceback

            traceback.print_exc()
            if out:
                _write_json(out / "progress.json", {"status": "failed", "error": f"{type(ex).__name__}: {ex}"})
            os._exit(1)
        if not out:
            print(json.dumps(st, default=str))
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)  # a question still stuck past its budget must not keep the process alive
    st = finish(json.loads(Path(a.result).read_text()))
    rows = st.get("rows") or []
    data = to_xlsx(rows, st.get("file", "")) if a.fmt == "xlsx" else to_csv(rows, st.get("file", "")).encode()
    if a.out:
        Path(a.out).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
