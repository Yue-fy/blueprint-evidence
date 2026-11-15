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
# rooms measured per sheet (80 cut the unseen DfE Secondary L00 at 80 of 120 labels in reading order: Dining Hall,
# Kitchen, Library had no row and nothing said so); rooms over the limit are named in state['rooms_cut']
MAX_ROOMS = int(os.environ.get("STOA_TAKEOFF_MAX_ROOMS", "200"))
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
QUANTITY_LABEL = {"area": "Floor area", "baseboard": "Skirting (baseboard)", "doors": "Doors", "windows": "Windows",
                  "slab_area": "Slab area", "rooms": "Rooms labelled", "area_sum": "Net floor area",
                  "baseboard_sum": "Skirting total", "baseboard_dry": "Skirting total without wet rooms",
                  "door_leaves": "Door leaves", "wall_length": "Wall length", "wall_area": "Wall area (one face)"}

# ------------------------------------------------------------------------------------------------ paths and caching


def uploads_dir() -> Path:
    """Where the web middleware keeps uploads (web/mock/stoaApi.ts: STOA_UPLOADS, default <tmp>/stoa-uploads)."""
    return Path(os.environ.get("STOA_UPLOADS") or Path(tempfile.gettempdir()) / "stoa-uploads")


def cache_dir() -> Path:
    d = Path(os.environ.get("STOA_TAKEOFF_CACHE") or Path(tempfile.gettempdir()) / "stoa-takeoff")
    d.mkdir(parents=True, exist_ok=True)
    return d


_UPLOAD = re.compile(r"^upload/([0-9a-f]{12})/(?:([a-z0-9_]+)/)?([^/\\]+)$")


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
        # 'upload/<id>/<style>/<name>' (a picture the upload guessed a dataset style for, e.g. cubicasa5k) is stored at
        # <uploads>/<id>/data/<style>/<name> (web/mock/stoaApi.ts uploadAbs; F8 22:42)
        m = _UPLOAD.match(path)
        if not m:
            raise ValueError("not an upload path (upload/<id>/<name>)")
        root = uploads_dir().resolve()
        p = (root / m.group(1) / "data" / m.group(2) / m.group(3)) if m.group(2) else (root / m.group(1) / m.group(3))
        p = p.resolve()
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


# 'wall' only as a wall's own label: DfE L00 prints 'Enclosed / Walkway' (118.98 m² on the PDF and the clean PNG) and the
# visitor's PNG OCR read 'Enclosed Wall' — a real corridor the 22:11 rule dropped as a wall
_OUTSIDE_RX = re.compile(r"^\s*walls?\s*$|\b(?:(?:retaining|existing|party|curtain|boundary|garden|perimeter|external|"
                         r"new|demolished)\s+walls?|canopy|canopies|site|courtyard|playground|car\s*park|parking|boundary|fence|"
                         r"landscap\w*|garden|external\s+(?:area|play|space|store)|outside|roof\s+(?:plan|level)|"
                         r"(?:asunto\s*)?piha|portti|pihavarasto)\b",
                         re.I)


def _room_name_ok(text: str, fi: bool = False) -> bool:
    t = _clean(text)
    if fi and fi_code(t) or re.fullmatch(r"(?i)w\.?\s*c\.?", t):  # a Finnish room code ('OH', 'MH 12,5') / 'WC'
        return True
    words = t.split()
    if not 1 <= len(words) <= 6 or len(t) > 48 or len(t) < 3:
        return False
    letters = sum(c.isalpha() for c in t)
    if letters < 3 or letters < 0.6 * sum(not c.isspace() for c in t):
        return False
    if _TITLE_RX.search(t) or _NOT_ROOM_RX.search(t) or t.endswith(("?", "!")) or str(text).rstrip().endswith("."):
        return False
    if _OUTSIDE_RX.search(t):  # a wall, a canopy, the site: not a floor area (W5 21:56: 'Enclosed Wall' = 119 m²)
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


def room_labels(pdf: str, page: int = 0, limit: int | None = MAX_ROOMS) -> list[dict]:
    """The rooms printed on a PDF sheet: [{name, key, pt, bbox, copy, copies}] (pdf_pt), in reading order; the first
    `limit` (None = all)."""
    from stoa.evidence import lineplan

    from stoa.evidence.linework import text_lines

    try:
        lines = list(text_lines(pdf, page))
    except Exception:  # noqa: BLE001
        lines = []
    fi = fi_sheet(t for t, _ in lines)  # a Finnish apartment plan: 'OH' / 'MH' / 'K' over the room's m²
    labs = _merge_open_parens(lineplan.labels(pdf, page))
    if not fi:  # 'WC' alone (2 letters: lineplan skips it) is a room name on any sheet (Esplanades floor 1 'WC' = IfcSpace WC 109)
        have = [lab["bbox"] for lab in labs]
        labs += [{"text": "WC", "bbox": list(b), "pt": ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)} for t, b in lines
                 if re.fullmatch(r"(?i)\s*w\.?\s*c\.?\s*", str(t)) and not any(
                     h[0] <= (b[0] + b[2]) / 2 <= h[2] and h[1] <= (b[1] + b[3]) / 2 <= h[3] for h in have)]
    if fi:  # codes are 1-2 letters: lineplan.labels skips them (and would stack 'ET' under '45.5 m²'): one per line
        labs = [lab for lab in labs if not fi_code(lab["text"])] + fi_lines(lines)
    vs = _plan_views(pdf, page)
    out = []
    for lab in labs:
        name = _clean(lab["text"])
        if not _room_name_ok(lab["text"], fi=fi):
            continue
        cx, cy = lab["pt"]
        if vs and not any(v.contains(cx, cy, pad=4) for v in vs):
            continue
        r = {"name": name, "key": name.lower(), "pt": [round(cx, 2), round(cy, 2)],
             "bbox": [round(v, 2) for v in lab["bbox"]]}
        inline = _inline_area(name) if fi else None  # 'Oh 15,0m2', 'Keittiö 10,4m²': the area on the label's line
        if re.fullmatch(r"(?i)w\.?\s*c\.?", name):  # a bare 'WC': asked by name it is ambiguous (INVA-WC, Female WC)
            r["by_label"] = True
        if fi and fi_code(name):
            code = fi_name(name) or name
            meaning = fi_code(name)[1]
            r.update(name=f"{code} ({meaning})", key=f"{code} ({meaning})".lower(), code=code, fi=True, by_label=True)
            if code.lower() != _FI_AREA.sub("", name).strip().lower():
                r["ocr_name"] = name
        elif inline:
            r.update(name=inline[1], key=inline[1].lower(), code=inline[1], fi=True, by_label=True)
        if r.get("fi") and inline:
            r["printed"] = {"value": inline[0], "text": inline[2], "bbox": r["bbox"]}
        elif r.get("fi") and _FI_AREA.search(name) and re.search(r"\d[.,]\d", name):  # 'MH 10.0 m²' on one line
            m = re.search(r"(\d{1,3}[.,]\d{1,2})", name)
            r["printed"] = {"value": float(m.group(1).replace(",", ".")), "text": m.group(1), "bbox": r["bbox"]}
        out.append(r)
    if fi:  # the m² printed under / after each name: nearest first, each number used once (S: '2.5 m²' up-right)
        fi_pair([r for r in out if not r.get("printed")], lines)
        for r in out:
            if r.get("printed") and not r.get("fi"):  # 'Keittiö' over '12,0m2': a word room with its printed area
                r.update(fi=True, by_label=True, code=r["name"])
        try:  # a code on the page's edge with no area: the next flat, cut off by the scan (10712: 'H', 'H' at x = 8 pt)
            import pymupdf

            with pymupdf.open(pdf) as doc:
                W, H = doc[page].rect.width, doc[page].rect.height
            out = [r for r in out if r.get("printed") or not r.get("fi") or
                   (0.03 * W < r["bbox"][0] and r["bbox"][2] < 0.97 * W and 0.03 * H < r["bbox"][1] and
                    r["bbox"][3] < 0.97 * H)]
        except Exception:  # noqa: BLE001
            pass
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
        r["by_label"] = r.get("by_label") or r["copies"] > 1 or len(stems[_stem(r["key"])]) > 1
    return out if limit is None else out[:limit]


_INLINE_AREA = re.compile(r"^(.*?[^\W\d_].*?)[\s:=]+\(?((\d{1,3}[.,]\d{1,2})\s*(?:m2|m²|m\^2)?|(\d{1,3})\s*(?:m2|m²|m\^2))\)?$",
                          re.I)


def _inline_area(name: str) -> tuple[float, str, str] | None:
    """'Keittiö 10,4m²' -> (10.4, 'Keittiö', '10,4m²'); a whole number needs its unit ('Mh 9 m2'); None otherwise."""
    m = _INLINE_AREA.match(" ".join(str(name).split()))
    if not m:
        return None
    v = float((m.group(3) or m.group(4)).replace(",", "."))
    rest = m.group(1).strip(" .,:;=")
    if not 0.5 <= v <= 400 or not re.search(r"[^\W\d_]{2,}", rest) or re.search(r"\+|\d\s*h\b", rest, re.I):
        return None  # '2H+K+S 55,0 m²' is the flat's type + total
    return v, rest, m.group(2)



def fi_flat_totals(pdf: str, page: int = 0) -> list[dict]:
    """A Finnish flat's printed type + total ('2H+K+S' over '55,0 m²', '3h+k+s 85 m2'): the huoneistoala, a printed
    area-table row 'Flat 2H+K+S, total (printed)' (inside the flat's walls, partitions included: more than the rooms)."""
    from stoa.evidence.linework import text_lines

    try:
        lines = list(text_lines(pdf, page))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for text, b in lines:
        t = " ".join(str(text).split())
        m = re.match(r"^(\d\s*h\s*\+[a-zåäö+\s]*?)\s*[=:]?\s*(?:(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:m2|m²))?$", t, re.I)
        if not m or not re.search(r"\+\s*[a-zåäö]", m.group(1), re.I):
            continue
        v, box, raw = m.group(2), list(b), t
        if v is None:  # the total on the next line ('55.0m²')
            h = max(1.0, b[3] - b[1])
            for t2, b2 in lines:
                m2 = re.match(r"^(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:m2|m²)$", " ".join(str(t2).split()), re.I)
                if m2 and -0.4 * h <= b2[1] - b[3] <= 1.2 * h and b2[0] <= (b[0] + b[2]) / 2 <= b2[2] + h:
                    v, box, raw = m2.group(1), [min(b[0], b2[0]), b[1], max(b[2], b2[2]), b2[3]], f"{t} {t2}"
                    break
        if v is None:
            continue
        val = float(v.replace(",", "."))
        if 10 <= val <= 400:
            out.append({"label": f"Flat {m.group(1).replace(' ', '').upper()}, total (printed)", "value": val,
                        "unit": "m2", "bbox": [round(c, 1) for c in box], "text": raw})
    return out


_FI_NUM = re.compile(r"^\(?(\d{1,3}[.,]\d{1,2})\)?\s*(?:m2|m²)?$", re.I)


def fi_lines(lines) -> list[dict]:
    """A Finnish sheet's room codes, one label per text line ('KH', 'MH 10.0 m²', '1200 VH' = a dimension OCR'd into
    the code's line); lowercase 1-2 letter cabinet codes ('sk', 'hk', 'jk', 'vk') are furniture, not rooms."""
    out = []
    for text, b in lines:
        t = re.sub(r"^\d{3,5}\s+", "", " ".join(str(text).split()))
        if not fi_code(t):
            continue
        head = _FI_AREA.sub("", t).strip()
        if head.islower() and len(head.replace(".", "")) <= 2:
            continue
        out.append({"text": t, "bbox": list(b), "pt": ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)})
    return out


def fi_pair(rooms: list[dict], lines) -> None:
    """Give each code room the area number printed nearest under / after it (greedy by distance, a number once)."""
    cands = []
    for i, r in enumerate(rooms):
        for j, (d, pr) in enumerate(_fi_numbers(r["bbox"], lines)):
            cands.append((d, i, pr))
    taken_r, taken_n = set(), set()
    for d, i, pr in sorted(cands, key=lambda c: c[0]):
        key = tuple(pr["bbox"])
        if i in taken_r or key in taken_n:
            continue
        rooms[i]["printed"] = pr
        taken_r.add(i)
        taken_n.add(key)


def _fi_numbers(bbox, lines) -> list[tuple[float, dict]]:
    x0, y0, x1, y1 = bbox
    h = max(1.0, y1 - y0)
    out = []
    for text, b in lines:
        m = _FI_NUM.match(" ".join(str(text).split()))
        if not m:
            continue
        bx0, by0, bx1, by1 = b
        below = -0.4 * h <= by0 - y1 <= 1.3 * h and (abs(bx0 - x0) <= 1.5 * h or bx0 <= (x0 + x1) / 2 <= bx1)
        after = abs((by0 + by1) / 2 - (y0 + y1) / 2) <= 0.8 * h and 0 <= bx0 - x1 <= 2.5 * h
        v = float(m.group(1).replace(",", "."))
        if (below or after) and 0.5 <= v <= 400:
            out.append((abs(by0 - y1) + abs(bx0 - x0) if below else abs(bx0 - x1) + 0.5 * h,
                        {"value": v, "text": str(text).strip(), "bbox": [round(c, 2) for c in b]}))
    return out


def fi_printed(bbox, lines) -> dict | None:
    """The room's m² printed right under (or after) a Finnish room code ('OH' over '16,3'): {value, text, bbox}."""
    x0, y0, x1, y1 = bbox
    h = max(1.0, y1 - y0)
    best = None
    for text, b in lines:
        m = _FI_NUM.match(" ".join(str(text).split()))
        if not m:
            continue
        bx0, by0, bx1, by1 = b
        below = -0.4 * h <= by0 - y1 <= 0.9 * h and (abs(bx0 - x0) <= 1.5 * h or bx0 <= (x0 + x1) / 2 <= bx1)
        after = abs((by0 + by1) / 2 - (y0 + y1) / 2) <= 0.4 * h and 0 <= bx0 - x1 <= 1.5 * h
        if below or after:
            d = abs(by0 - y1) + abs(bx0 - x0)
            if best is None or d < best[0]:
                v = float(m.group(1).replace(",", "."))
                if 0.5 <= v <= 400:
                    best = (d, {"value": v, "text": str(text).strip(), "bbox": [round(c, 2) for c in b]})
    return best[1] if best else None


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


def _direct(solver: str, abs_path: str, virtual: str, question: str, page: int = 0) -> dict:
    """One reader alone (no cascade, no VLM fallback), evidence renamed to the virtual path like ask_upload does.
    A multi-page PDF: `openings` counts on the TAKEOFF's page only (its pick_pages would sum every page of the set
    for 'How many doors does the Kitchen have?'), pinned for this call."""
    from stoa.solvers import get_solver
    from stoa.solvers import openings as O

    orig = O.pick_pages
    if abs_path.lower().endswith(".pdf"):
        target = str(Path(abs_path).resolve())

        def pinned(pdf, q, *a, **k):
            if str(Path(pdf).resolve()) == target:
                return [int(page)], f"openings: page {int(page) + 1} (the takeoff's page)"
            return orig(pdf, q, *a, **k)

        O.pick_pages = pinned
    try:
        out = get_solver(solver).solve(abs_path, question).model_dump()
    finally:
        O.pick_pages = orig
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
            box["out"] = _direct(solver, abs_path, virtual, question, page) if solver else _asker()(req)
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
    # why it is rough, for the Review list: on a traced scan only one line set is read (E1 3c90bdd8 skips 'all'), and a
    # single reading was within 2 % of the vector PDF's room for 1 of 12 DfE scan rooms (agreeing ones: 6 of 9)
    rough = None if m.get("support", 0) >= 3 else (
        f"{m.get('support', 0)} of {m.get('n_readings', 0)} readings of the drawn lines give this outline "
        "(3 that agree make it a best estimate)")
    if "bridge@" in str(m.get("variant")):
        conf = min(conf, 0.4)
    assum = [f"scale: 1:{n:g} printed on the sheet", f"copy {room['copy']} of {room['copies']} rooms labelled "
             f"'{room['name']}', outline traced from the wall lines around its own label"]
    if rough:
        base = {**base, "rough": rough}
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
                     "parts": {"perimeter_m": sk.get("perimeter_m"),
                               "openings_m": [round(float(o[2]), 3) for o in sk.get("openings") or []]},
                     "assumptions": [*assum, f"perimeter {sk['perimeter_m']:.2f} m minus {n_open} opening(s)"],
                     "evidence": [{"file": virtual, "page": page, "bbox": bb, "bbox_space": "pdf_pt",
                                   "polyline": run, "note": f"baseboard run of {room['name']} ({room['copy']}/"
                                                            f"{room['copies']})"} for run in (sk.get("runs") or [])[:6]]})
    return rows


def _inside(pt, poly) -> bool:
    x, y = pt
    ins = False
    for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / ((by - ay) or 1e-9):
            ins = not ins
    return ins


CALIB_AGREE = 0.03  # two printed rooms 'agree' on the scale when their 1:N differ by <= 3 % (= ~6 % in area)
# measured (experiments/f2/fi_calib_bench.py, leave-one-out on 3 CubiCasa scans): a room traced at the scale the OTHER
# printed rooms give came within 10 % of its own printed m² in 1 of 10 cases (scan outlines catch furniture / leak): so
# such rows are ALWAYS Review; the printed m² are the takeoff's numbers on these plans
CALIB_CONF = 0.3
CALIB_NOTE = ("traced on a scan at a scale derived from other rooms' printed areas: rough (tested: within 10 % in 1 of "
              "10 rooms); measure it with Measure")


def calibrate_printed(pdf: str, page: int, rooms: list[dict], n0: float = 100.0) -> dict | None:
    """No printed 1:N, but rooms print their m² (Finnish plans: 'MH' over '12,5'): the scale that makes each traced
    outline match its printed area. Rooms that trace to ONE outline (open plan: OH + K + ET) count as one region with
    the printed areas summed, and only when every room label inside it prints its area; two passes (the tracer's door
    gaps are in metres). {'n', 'groups': [{names, printed, n}], 'agree': k, 'how'} or None."""
    from stoa.evidence import lineplan

    pr = [r for r in rooms if r.get("printed")]
    if not pr:
        return None
    n, out = n0, None
    for _ in range(2):
        traced = []
        for r in rooms:
            try:
                m = lineplan._measure(pdf, page, tuple(r["pt"]), n, tuple(r["bbox"]))
            except Exception:  # noqa: BLE001
                m = None
            if m and m.get("area_m2"):
                traced.append((r, m))
        groups: list[dict] = []
        for r, m in traced:
            g = next((g for g in groups if abs(g["a"] - m["area_m2"]) <= 0.01 * g["a"] and _inside(r["pt"], g["poly"])),
                     None)
            if g is None:
                groups.append({"a": m["area_m2"], "poly": [tuple(p) for p in m["polygon"]], "rooms": [r]})
            else:
                g["rooms"].append(r)
        for g in groups:  # every room label inside the outline (also ones whose own trace failed) must print its area
            inside = [r for r in rooms if _inside(r["pt"], g["poly"])]
            g["ok"] = bool(inside) and all(r.get("printed") for r in inside)
            g["printed"] = sum(r["printed"]["value"] for r in inside if r.get("printed"))
            g["names"] = [r.get("code") or r["name"] for r in inside]
            g["n"] = n * (g["printed"] / g["a"]) ** 0.5 if g["ok"] and g["printed"] else None
        gs = [g for g in groups if g["n"]]
        if not gs:
            return out
        # the densest cluster (most rooms agreeing, then most printed m²), not the median: one
        # leaked or furniture-cut outline per plan is common (3172: 1:73-1:282 on a first pass at 1:100)
        best = max(gs, key=lambda c: (sum(abs(g["n"] - c["n"]) / c["n"] <= CALIB_AGREE for g in gs),
                                      sum(g["printed"] for g in gs if abs(g["n"] - c["n"]) / c["n"] <= CALIB_AGREE)))
        agree = [g for g in gs if abs(g["n"] - best["n"]) / best["n"] <= CALIB_AGREE]
        if len(gs) >= 2 and len(agree) < 2:  # printed rooms disagree on the scale: no scale
            return {"n": None, "groups": [{"names": g["names"], "printed": g["printed"], "n": round(g["n"], 1)}
                                          for g in groups if g["n"]], "agree": 0,
                    "how": "the printed room areas disagree on the scale"}
        n = sum(g["n"] * g["printed"] for g in agree) / sum(g["printed"] for g in agree)
        out = {"n": round(n, 2), "agree": len(agree),
               "groups": [{"names": g["names"], "printed": round(g["printed"], 2), "n": round(g["n"], 1)}
                          for g in agree],
               "outliers": [{"names": g["names"], "printed": round(g["printed"], 2), "n": round(g["n"], 1)}
                            for g in gs if g not in agree]}
    used = " · ".join(f"{'+'.join(g['names'])} {g['printed']:g} m²" for g in out["groups"])
    out["how"] = (f"from the printed room areas ({used}: {out['agree']} agree within {CALIB_AGREE:.0%})"
                  if out["agree"] >= 2 else f"from ONE printed room area ({used}): check it")
    return out


_NTS_RX = re.compile(r"\b(ei\s+mittakaava\w*|suuntaa-?\s*antava|ej\s+skalenlig\w*|not\s+to\s+scale)\b", re.I)


def _not_to_scale(pdf: str, page: int) -> str | None:
    """The plan's own 'not to scale' words (Finnish / Swedish / English; never the UK title block's 'DO NOT SCALE')."""
    try:
        from stoa.evidence.linework import text_lines

        for t, _ in text_lines(pdf, page):
            m = _NTS_RX.search(" ".join(str(t).split()))
            if m:
                return " ".join(str(t).split())[:60]
    except Exception:  # noqa: BLE001
        return None
    return None


def fi_rows(rows: list[dict], rooms: list[dict], virtual: str, page: int, calib: dict | None) -> None:
    """Finnish plan rooms after tracing: a printed m² IS the room's area (exact to the drawing, the traced outline
    stays as evidence + check); a room whose traced outline also holds printed rooms' codes (open plan: ET with OH + K)
    = the outline minus their printed areas; a room traced at a scale derived from printed areas says so; skirting
    measured round an open plan (several codes inside one outline) goes to Review."""
    by = {(r["name"], r.get("copy", 1)): r for r in rooms if r.get("fi")}
    areas = [x for x in rows if x.get("section") == "room" and x.get("quantity") == "area" and
             (x.get("item"), x.get("copy", 1)) in by]
    used = {nm for g in (calib or {}).get("groups") or [] for nm in g.get("names") or []}
    if calib and calib.get("n"):  # no printed 1:N: every traced row says where its scale came from
        for y in rows:
            if y.get("section") == "room" and (y.get("item"), y.get("copy", 1)) in by and y.get("quantity") != "area":
                y["assumptions"] = [f"scale {calib['how']}: 1:{calib['n']:g} on this traced copy"
                                    if str(a).startswith("scale: 1:") else a for a in y.get("assumptions") or []]
                if isinstance(y.get("value"), (int, float)) and y.get("quantity") == "baseboard":
                    y["confidence"] = min(y.get("confidence") or 0, CALIB_CONF)
    inside: dict[int, list[dict]] = {}
    for x in areas:
        poly = next((e.get("polygon") for e in x.get("evidence") or [] if e.get("polygon")), None)
        if poly and isinstance(x.get("value"), (int, float)):
            me = by[(x["item"], x.get("copy", 1))]
            pl = [tuple(p) for p in poly]
            inside[id(x)] = [r for r in rooms if r is not me and r.get("pt") and _inside(r["pt"], pl)]
    for x in areas:
        r = by[(x["item"], x.get("copy", 1))]
        traced = x.get("value") if isinstance(x.get("value"), (int, float)) else None
        if calib and calib.get("n") and traced is not None:
            x["assumptions"] = [a for a in x.get("assumptions") or [] if not a.startswith("scale: 1:")]
            x["assumptions"].insert(0, f"scale {calib['how']}: 1:{calib['n']:g} on this traced copy")
            x["assumptions"].append(CALIB_NOTE)
            x["confidence"] = min(x.get("confidence") or 0, CALIB_CONF)
        mates = inside.get(id(x)) or []
        if mates:  # its skirting ran round the whole open plan: not this room's
            for y in rows:
                if y.get("section") == "room" and y.get("quantity") == "baseboard" and y.get("item") == x["item"] \
                        and y.get("copy", 1) == x.get("copy", 1) and isinstance(y.get("value"), (int, float)):
                    y["confidence"] = min(y.get("confidence") or 0, 0.3)
                    y["assumptions"] = [*(y.get("assumptions") or []), "measured round the open-plan outline that also "
                                        "holds " + ", ".join(m.get("code") or m["name"] for m in mates)]
        p = r.get("printed")
        if p:
            ev = [{"file": virtual, "page": page, "bbox": p["bbox"], "bbox_space": "pdf_pt",
                   "note": f"printed area '{p['text']}' of {r.get('code')}"}]
            ev += [dict(e, note=f"traced outline (check): {traced:g} m²") for e in x.get("evidence") or []]
            x.update(value=p["value"], confidence=0.85, status="ok", evidence=ev,
                     method="printed with the room code (Finnish plan)",
                     assumptions=[f"area as printed with '{r.get('code')}': '{p['text']}'"
                                  f"{'' if re.search(r'm(?:2|²)', p['text']) else ' m²'} (Finnish plans print each "
                                  "room's floor area with its name)"])
            if r.get("code") in used and calib and calib.get("n"):
                x["assumptions"].append("its printed area set the drawing's scale (with the other printed rooms)"
                                        if calib.get("agree", 0) >= 2 else "its printed area set the drawing's scale")
            elif traced is not None and not mates:
                d = (traced - p["value"]) / p["value"]
                x["check"] = {"value": traced, "diff_pct": round(100 * d, 1), "agree": abs(d) <= 0.02,
                              "how": "traced from the wall lines at the code"}
            elif mates:
                x["assumptions"].append("open plan: its traced outline also holds " +
                                        ", ".join(m.get("code") or m["name"] for m in mates))
            continue
        if traced is None or not mates:
            continue
        if any(not m.get("printed") for m in mates):
            x["confidence"] = min(x.get("confidence") or 0, 0.3)
            x["assumptions"] = [*(x.get("assumptions") or []), "its traced outline also holds " +
                                ", ".join(m.get("code") or m["name"] for m in mates) + " (no printed area to subtract)"]
            continue
        rest = round(traced - sum(m["printed"]["value"] for m in mates), 2)
        if rest >= 0.5:
            x["assumptions"] = [*(x.get("assumptions") or []), f"open plan: the outline {traced:g} m² minus the printed "
                                + " + ".join(f"{m.get('code')} {m['printed']['value']:g}" for m in mates)]
            x["value"] = rest
            x["method"] = "open-plan outline minus the printed rooms in it"
        else:
            x["confidence"] = min(x.get("confidence") or 0, 0.3)


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
    pt_m = 1.0 / (n * 0.0254 / 72)  # PDF points per metre
    pad = 0.25 * pt_m
    by_key: dict = {}
    for r in rows:
        if r.get("section") == "room":
            by_key.setdefault((r["item"], r.get("copy", 1)), {})[r["quantity"]] = r
    side = os.environ.get("STOA_TAKEOFF_WIN_SIDE", "1") != "0"
    owner: dict = {}
    if side:  # each window to the ONE room on its inner side of an outside wall (F6 23:21: inner rooms got windows)
        polys = []
        for (item, copy), d in by_key.items():
            pg = next((e.get("polygon") for e in (d.get("area") or {}).get("evidence") or [] if e.get("polygon")), None)
            try:
                polys.append(((item, copy), Polygon(pg).buffer(0))) if pg and len(pg) >= 3 else None
            except Exception:  # noqa: BLE001
                pass
        owner = window_owners([x[0] for x in wins], polys, pt_m)
        rule = rule.replace("touching the room's traced outline (+0.25 m)",
                            "in an outside wall of the room (its traced outline +0.25 m on one side, no traced room "
                            "across the wall)")
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
        hit = [x for i, x in enumerate(wins) if outline.intersects(x[0])
               and (not side or owner.get(i, "free") in ("free", (item, copy)))]
        row = {"section": "room", "item": item, "copy": copy, "copies": (a or {}).get("copies", 1),
               "quantity": "windows", "unit": "count", "value": len(hit), "confidence": conf, "status": "ok",
               "method": how, "seconds": 0,
               "assumptions": [rule + ("; a window with a room on both sides (an inner wall) or inside one room "
                                       "counts for none" if side else "; a window between two rooms counts for both"),
                               f"scale 1:{n:g}"],
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


def window_owners(boxes: list, polys: list, pt_m: float) -> dict:
    """{window index: room key | None}: the room a window box belongs to by the wall it sits in. Across the box's short
    side (the wall's thickness) a point 0.3 / 0.6 m beyond each face is looked up in the traced room outlines: a room on
    exactly one side and none on the other = an outside wall of THAT room; a room on both sides (an inner wall: the toilet
    between hall and bedroom, Esplanades) or the same room on both (a symbol inside a lift shaft) = None (no room's
    window). A box with no traced room on either side is left 'free' (missing from the dict: the plain touching rule
    decides). Square boxes try both orientations."""
    out: dict = {}

    def at(x, y):
        from shapely.geometry import Point

        p = Point(x, y)
        return {k for k, pg in polys if pg.contains(p)}

    for i, b in enumerate(boxes):
        x0, y0, x1, y1 = b.bounds
        cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
        axes = ["v"] if w > 1.25 * h else ["h"] if h > 1.25 * w else ["v", "h"]  # 'v': wall runs along x
        verdicts = []
        for ax in axes:
            sides = []
            for sgn in (-1, 1):
                got: set = set()
                for dm in (0.3, 0.6):
                    dd = dm * pt_m
                    got = at(cx, cy + sgn * (h / 2 + dd)) if ax == "v" else at(cx + sgn * (w / 2 + dd), cy)
                    if got:
                        break
                sides.append(got)
            a, c = sides
            if a and c:
                verdicts.append(None)  # inner wall, or inside one room
            elif a or c:
                k = a or c
                verdicts.append(next(iter(k)) if len(k) == 1 else None)
        own = [v for v in verdicts if v is not None]
        if own:
            out[i] = own[0]
        elif verdicts:
            out[i] = None
    return out


def _reown_windows(st: dict, rows: list[dict]) -> int:
    """Read time: per-room window rows stored by an older run (the touching rule: 'a window between two rooms counts for
    both') get the outside-wall rule (window_owners) from their OWN evidence boxes and the rooms' traced outlines, so the
    cached catalogue takeoffs need no re-run. Vector sheets only (boxes and outlines in PDF points). Returns the rows
    changed."""
    if os.environ.get("STOA_TAKEOFF_WIN_SIDE", "1") == "0" or st.get("kind") != "vector-pdf":
        return 0
    wr = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "windows"
          and str(r.get("method") or "").startswith(("door_detect symbols", "window units drawn"))
          and any("counts for both" in str(a) for a in r.get("assumptions") or [])]
    n = next((float(m.group(1)) for r in wr for a in r.get("assumptions") or []
              for m in [re.search(r"scale 1:([\d.]+)", str(a))] if m), None)
    if not wr or not n:
        return 0
    try:
        from shapely.geometry import Polygon
        from shapely.geometry import box as sbox
    except Exception:  # noqa: BLE001
        return 0
    polys = []
    for r in rows:
        if r.get("section") == "room" and r.get("quantity") == "area":
            pg = next((e.get("polygon") for e in r.get("evidence") or [] if e.get("polygon")), None)
            try:
                if pg and len(pg) >= 3:
                    polys.append(((r["item"], r.get("copy", 1)), Polygon(pg).buffer(0)))
            except Exception:  # noqa: BLE001
                pass
    keys: dict = {}
    for r in wr:
        for e in r.get("evidence") or []:
            if e.get("bbox") and len(e["bbox"]) == 4:
                keys.setdefault(tuple(e["bbox"]), len(keys))
    if not keys:
        return 0
    boxes = [None] * len(keys)
    for bb, i in keys.items():
        boxes[i] = sbox(*bb)
    owner = window_owners(boxes, polys, 1.0 / (n * 0.0254 / 72))
    changed = 0
    for r in wr:
        me = (r.get("item"), r.get("copy", 1))
        ev = [e for e in r.get("evidence") or []
              if not (e.get("bbox") and len(e["bbox"]) == 4) or owner.get(keys[tuple(e["bbox"])], "free") in ("free", me)]
        changed += len(ev) != len(r.get("evidence") or [])
        r["evidence"] = ev
        r["value"] = sum(1 for e in ev if e.get("bbox"))
        r["assumptions"] = [str(a).replace("touching the room's traced outline (+0.25 m)",
                                           "in an outside wall of the room (its traced outline +0.25 m on one side, no "
                                           "traced room across the wall)")
                            .replace("; a window between two rooms counts for both",
                                     "; a window with a room on both sides (an inner wall) or inside one room counts "
                                     "for none") for a in r.get("assumptions") or []]
    return changed


def ifc_spaces(pdf: str, virtual: str, rows: list[dict], page: int = 0) -> dict | None:
    """A plan drawn FROM an IFC model (IfcConvert: every IfcSpace a filled outline with its name + number, E1's
    stoa.evidence.ifcplan_pdf; Maleva 18) links each takeoff room to the model's own room: the space outline that holds
    the room's label. Its area becomes the row's value (the model's net room, 63.82 vs 63.84 m² in the IFC) and our
    trace from the wall lines stays as the cross-check ('traced 5.99 m², +2.7 %'); the row takes the space's name +
    number ('TUALETTRUUM 5-3'); a space no label landed in is added as a room. None when the sheet has no spaces."""
    if page or not pdf.lower().endswith(".pdf"):
        return None
    try:
        from shapely.geometry import Point, Polygon

        from stoa.evidence.ifcplan_pdf import space_polygons

        sp = [s for s in space_polygons(pdf) if s.get("m2") and s["m2"] > 0.2]
    except Exception:  # noqa: BLE001
        return None
    if len(sp) < 3:
        return None
    polys = []
    for s in sp:
        try:
            pl = Polygon(s["polygon"])
            polys.append((s, pl if pl.is_valid else pl.buffer(0)))
        except Exception:  # noqa: BLE001
            continue
    by_key: dict = {}
    for r in rows:
        if r.get("section") == "room":
            by_key.setdefault((r["item"], r.get("copy", 1)), []).append(r)
    used, agree, differ = set(), 0, 0
    for (item, copy), rs in list(by_key.items()):
        a = next((r for r in rs if r.get("quantity") == "area"), None)
        lb = (a or {}).get("label_box") or next((r.get("label_box") for r in rs if r.get("label_box")), None)
        if a is None or not lb:
            continue
        c = Point((lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2)
        hit = [(s, pl) for s, pl in polys if pl.contains(c)]
        if not hit:
            continue
        s, pl = min(hit, key=lambda h: h[1].area)
        used.add(id(s))
        m2 = float(s["m2"])
        ours = a.get("value") if isinstance(a.get("value"), (int, float)) and a.get("value") else None
        diff = (ours - m2) / m2 if ours else None
        ok = diff is not None and abs(diff) <= 0.02
        agree += ok
        differ += (diff is not None and not ok)
        name = s.get("label") or item
        a.update(value=round(m2, 2), unit="m2", confidence=0.85 if ok else 0.8, status="ok",
                 method="IfcSpace outline drawn on the sheet (the model's own room)",
                 check={"value": ours, "diff_pct": round(100 * diff, 1) if diff is not None else None, "agree": ok,
                        "how": "traced from the wall lines around the label (lineplan)"},
                 assumptions=[f"area of the model's space '{name}' as drawn on this sheet at 1:{s['scale']}"
                              + ("" if s.get("scale_printed") else " (scale assumed)"),
                              ("cross-check: traced from the walls " + (f"{ours:g} m² ({100 * diff:+.1f} %)"
                                                                        if ours else "gave no reading"))
                              + (" ✓ agrees within 2 %" if ok else "")],
                 evidence=[{"file": virtual, "page": page, "bbox": s["bbox"], "bbox_space": "pdf_pt",
                            "polygon": s["polygon"], "note": f"IfcSpace {name}: {m2:g} m²"}]
                 + [dict(e, note=f"traced outline (cross-check): {ours:g} m²") for e in (a.get("evidence") or [])[:1]
                    if ours])
        a.pop("review", None)
        if name != item:  # the model's number makes repeated names unique ('ESIK 6-2')
            for r in rs:
                r.update(item=name, copy=1, copies=1)
    added = 0
    for s, pl in polys:
        if id(s) in used or not s.get("label"):
            continue
        rows.append({"section": "room", "item": s["label"], "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
                     "value": round(float(s["m2"]), 2), "confidence": 0.8, "status": "ok", "seconds": 0,
                     "method": "IfcSpace outline drawn on the sheet (the model's own room)",
                     "label_box": s["bbox"], "assumptions": [f"a model space with no room label found by the takeoff; "
                                                             f"area as drawn at 1:{s['scale']}"],
                     "evidence": [{"file": virtual, "page": page, "bbox": s["bbox"], "bbox_space": "pdf_pt",
                                   "polygon": s["polygon"], "note": f"IfcSpace {s['label']}: {s['m2']:g} m²"}]})
        added += 1
    return {"spaces": len(polys), "linked": len(used), "agree": agree, "differ": differ, "added": added}


def doors_by_room(pdf: str, virtual: str, rows: list[dict], page: int = 0, scale_n: float | None = None) -> int:
    """Per-room DOORS for rooms the by-name question could not ask (Group Room x10: one name, ten rooms): DR's swing
    arcs (stoa.solvers.openings find_swings / group_doors) and DR's own door-to-room rule (a doorway on an edge of the
    room's traced outline, 0.45 m tolerance) applied to each copy's outline. Local and fast (arcs read once)."""
    try:
        from stoa.evidence import linework as L
        from stoa.solvers import openings as O

        sc = L.sheet_scales(pdf, page)
        sw = O.find_swings(pdf, page, float(sc[0]) if sc else scale_n)  # scale_n: one derived from printed areas
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


_BUILDING_TOTAL_RX = re.compile(r"\b(?:g?ifa|gia|nia|gea|gross|overal[li]|total|sub-?total|site\s+area|building\s+area)\b",
                                re.I)
AREA_TAGS_MIN = 3  # printed room tags before they outrank the sheet's room labels (1-2 = a note / key plan)


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
        if not re.search(r"[^\W\d_]{2,}", re.sub(r"(?i)\b(?:sq\.?\s*ft|sf|sm|m2|m²|ft2|gsf|nsf|area)\b", " ", name)):
            continue  # numbers + an area unit ('095 SF', '6,092', '911 SF ,240 SF': a traced area table / key plan)
        if _BUILDING_TOTAL_RX.search(name):
            continue  # a sheet note 'Overall GIFA Ground: 2398m2' (X1's EFA scans): a building total, never a room tag
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


def tag_trace(pdf: str, virtual: str, page: int, t: dict, n: float | None, rows: list[dict]) -> None:
    """A printed room tag's room traced at its tag (measure_copy): the traced outline joins the tag's area row as
    evidence with a check (printed vs drawn), and its skirting becomes the row's baseboard unless a reader already
    answered it by name."""
    label = f"{t['name']} ({t['number']})" if t.get("number") else t["name"]
    bb = t.get("bbox") or []
    if len(bb) != 4:
        return
    got = measure_copy(pdf, virtual, page, {"name": t["name"], "copy": t["copy"], "copies": t["copies"], "bbox": bb,
                                            "pt": [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]}, n)
    area = next((r for r in rows if r.get("section") == "room" and r.get("quantity") == "area" and
                 r.get("item") == label and r.get("copy") == t["copy"]), None)
    tr = next((g for g in got if g["quantity"] == "area"), None)
    if area is not None and tr is not None and isinstance(tr.get("value"), (int, float)) and tr["value"] > 0:
        pv = area.get("value")
        diff = (tr["value"] - pv) / pv if isinstance(pv, (int, float)) and pv else None
        area["evidence"] = list(area.get("evidence") or []) + [dict(e, note=f"traced outline (check): {tr['value']:g} m²")
                                                               for e in tr.get("evidence") or []]
        area["check"] = {"value": tr["value"], "diff_pct": round(100 * diff, 1) if diff is not None else None,
                         "agree": diff is not None and abs(diff) <= 0.02, "how": "traced from the wall lines at the tag"}
    have = any(r.get("section") == "room" and r.get("quantity") == "baseboard" and r.get("item") == label and
               r.get("copy") == t["copy"] for r in rows)
    sk = next((g for g in got if g["quantity"] == "baseboard"), None)
    if sk is not None and not have and not _no_skirting(t["name"]):
        sk.update(item=label)
        rows.append(sk)


# A PIPING plan (Stoa 16:10: 'if you want to impress us, show that your system understands piping plans too'; their
# USACE M-101A example): no rooms, no slabs, but printed equipment tags. Its takeoff = the tagged equipment by family,
# counted by Erin's piping reader (stoa.solvers.piping: PDF text; piping_image: a picture's OCR layer), one evidence box
# per tag. Numbered tags (CV-3) count distinct IDs (a tag repeated in a note is one item, G1 D1 aw29o); unnumbered
# symbols (PG, MAV, a bare PS) count every printed occurrence. (family, label, distinct?)
PIPING_FAMILIES = (
    ("CV", "Check valves (CV-n)", True),
    ("FP", "Pumps (FP-n)", True),
    ("FSI", "Filter separators, issue (FSI-n)", True),
    ("FSR", "Filter separators, receipt (FSR-n)", True),
    ("FSCV", "FSCV-n valves", True),
    ("PS-", "Pipe support types (PS-n)", True),
    ("PG", "Pressure gauges (PG)", False),
    ("MAV", "Manual air vents (MAV)", False),
    ("PS", "Pressure sensors (PS)", False),
)
PIPING_MIN_TAGS = 5  # printed tags (of >= 2 families) before a sheet with no rooms / slabs is read as a piping plan
PIPING_PLAN_FAMILIES, PIPING_PLAN_TAGS = 3, 15  # this many: a piping plan even when its labels look like rooms


def _piping_page(abs_path: str, page: int, picture_src: str | None):
    """Erin's page model of the sheet: (page, space). A picture: its OCR layer (pixels) when it was read."""
    try:
        if picture_src:
            from stoa.solvers.piping_image import _page

            pg = _page(picture_src)
            return (pg, "image_px") if pg is not None else (None, "")
        if abs_path.lower().endswith(".pdf"):
            from stoa.solvers import piping as P

            pages = P._read_pdf(abs_path)
            return (pages[page], "pdf_pt") if 0 <= page < len(pages) else (None, "")
    except Exception:  # noqa: BLE001
        pass
    return None, ""


def piping_rows(abs_path: str, virtual: str, page: int = 0, picture_src: str | None = None) -> list[dict]:
    """The tagged-equipment rows of a piping plan ([] when the sheet prints fewer than PIPING_MIN_TAGS tags of >= 2
    families). Each row = one family: value, every counted tag as an evidence box, the IDs in its assumptions."""
    from stoa.solvers import piping as P

    pg, space = _piping_page(abs_path, page, picture_src)
    if pg is None:
        return []
    # tag numbers start at 1: a '-0' is an OCR misread (UI upload of Stoa's M-101A PNG read a 'PS-0': 24 types, gold 23)
    tags = [(t, w) for t, w in P._word_tags(pg) if not re.fullmatch(r"[A-Z]+-0+", t)]
    bare_ps = P._bare_ps_words(pg)
    fam = {}
    for f, label, distinct in PIPING_FAMILIES:
        if f == "PS":
            hits = [("PS", w) for w in bare_ps]
        elif f == "PS-":
            hits = [(t, w) for t, w in tags if t.startswith("PS-")]
        else:
            hits = [(t, w) for t, w in tags if t == f or t.split("-")[0] == f]
        if distinct:
            hits = list({t: w for t, w in reversed(hits)}.items())[::-1]  # first printed box of each ID
            hits.sort(key=lambda tw: (tw[0].split("-")[0], int(tw[0].split("-")[1]) if "-" in tw[0] and
                                      tw[0].split("-")[1].isdigit() else 0))
        if hits:
            fam[f] = (label, distinct, hits)
    if len(fam) < 2 or sum(len(h) for _, _, h in fam.values()) < PIPING_MIN_TAGS:
        return []
    px = space == "image_px"
    rows = []
    for f, (label, distinct, hits) in fam.items():
        ids = [t for t, _ in hits]
        rows.append({
            "section": "equipment", "item": label, "quantity": "count", "unit": "count", "value": len(hits),
            "confidence": 0.75 if px else 0.9, "status": "ok", "seconds": 0, "family": "PS-n" if f == "PS-" else f,
            "method": "printed equipment tags (piping reader" + (", the picture's OCR layer)" if px else ", PDF text)"),
            "assumptions": [f"counted {'distinct IDs' if distinct else 'every printed symbol label'}: "
                            + (", ".join(ids[:40]) + (" …" if len(ids) > 40 else "") if distinct else f"{len(ids)} x {f}"),
                            "only explicit printed tags on this sheet; an untagged item is not counted"
                            + ("; read from the picture's OCR: a misread tag changes the count" if px else "")],
            "evidence": [{"file": virtual, "page": None if px else page, "bbox": [round(v, 1) for v in w.bbox],
                          "bbox_space": space, "note": t} for t, w in hits[:MAX_MARKS]],
            **({"_px": True} if px else {})})
    return rows


def plan_items(abs_path: str, page: int, picture: bool = False, picture_src: str | None = None) -> dict:
    """What the takeoff will ask: {'kind', 'rooms', 'slabs', 'items': [{section, item, quantity, question, ...}]}.
    A sheet that calls out slab types is a foundation / slab plan: its takeoff = the slab types (its other text is
    notes, not rooms). A sheet with printed room tags: those rooms, their printed areas. Else the room labels."""
    is_pdf = abs_path.lower().endswith(".pdf")
    slabs = slab_types(abs_path, page) if is_pdf else []
    fi_rooms = None
    if is_pdf and not slabs and _is_fi_sheet(abs_path, page):
        # a Finnish apartment plan: its codes + printed m² beat the printed-tag reader, which took the flat's
        # '2H+KK+S 45.5 m²' and a fridge code 'jk' for rooms and missed KH / S / VH (cubicasa5k 10712)
        fi_rooms = [r for r in room_labels(abs_path, page, None) if not _VOID_RX.search(r["name"])]
        if not any(r.get("fi") for r in fi_rooms):
            fi_rooms = None
    # a PIPING plan prints many equipment tags of several families (Stoa's M-101A: 81 tags, 9 families): its labels
    # are line descriptions ('8" FROM BULK'), not rooms (before 02:00 the takeoff asked 298 room questions there, > 10 min)
    piping = piping_rows(abs_path, "", page, picture_src) if not slabs and fi_rooms is None else []
    pipe_plan = len(piping) >= PIPING_PLAN_FAMILIES and sum(r["value"] for r in piping) >= PIPING_PLAN_TAGS
    tags = area_tags(abs_path, page) if is_pdf and not slabs and fi_rooms is None and not pipe_plan else []
    if 0 < len(tags) < AREA_TAGS_MIN:
        # one or two 'tags' are no tagged plan (X1's EFA scans: the area note alone made the takeoff 1 room, the
        # vector twin 27-73): the room labels decide; the tags still count when no room label is found
        labs = [r for r in room_labels(abs_path, page, None) if not _VOID_RX.search(r["name"])]
        if len(labs) > len(tags):
            tags = []
    rooms = fi_rooms if fi_rooms is not None else [] if slabs or tags or not is_pdf or pipe_plan else \
        [r for r in room_labels(abs_path, page, None) if not _VOID_RX.search(r["name"])]
    cut = [r["name"] for r in rooms[MAX_ROOMS:]]  # over the limit: said, never dropped in silence
    rooms = rooms[:MAX_ROOMS]
    items = []
    for r in tags:
        items.append({"section": "room", "kind": "tag", "item": r["name"], "copy": r["copy"], "copies": r["copies"],
                      "quantity": "area", "tag": r})
        if r["copies"] == 1:
            for q, text, unit in ROOM_QS[1:]:
                items.append({"section": "room", "item": f"{r['name']} ({r['number']})" if r.get("number")
                              else r["name"], "copy": 1, "copies": 1, "quantity": q, "unit": unit,
                              "question": text.format(room=r["name"]), "label_box": r["bbox"]})
        # then the room's outline traced at its tag: skirting for repeated names (no question can name them; a
        # reading by name above is kept), the outline for doors / windows per room, printed vs drawn area check
        items.append({"section": "room", "kind": "tagtrace", "item": r["name"], "copy": r["copy"],
                      "copies": r["copies"], "tag": r})
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
    table = printed_table(abs_path, page, picture=picture_src) if (is_pdf or picture_src) and not pipe_plan else []
    if is_pdf and any(r.get("fi") for r in rooms):
        table += fi_flat_totals(abs_path, page)
    for t in table:
        items.append({"section": "printed", "kind": "printed", "item": t["label"], "quantity": "area", "row": t})
    for s in slabs:
        items.append({"section": "slab", "item": s["text"], "quantity": "slab_area", "unit": "m2",
                      "question": f"What is the area of the {s['text']}?", "label_box": s["bbox"]})
    if rooms_or_tags or table:
        piping = []  # a building plan with a few tags (a plant room's valves): its rooms are the takeoff
    for r in piping:
        items.append({"section": "equipment", "kind": "equipment", "item": r["item"], "quantity": "count", "row": r})
    for q, text, unit in SHEET_QS if not slabs and not piping else ():  # a slab / piping plan draws no doors, windows
        items.append({"section": "sheet", "item": "Whole sheet", "quantity": q, "unit": unit, "question": text})
    if not rooms_or_tags and not slabs and not table and not piping:
        items.append({"section": "sheet", "item": "Whole sheet", "quantity": "area", "unit": "m2",
                      "question": "What is the total floor area of this plan?"})
    kind = "vector-pdf" if is_pdf else "image"
    return {"kind": kind, "rooms": rooms_or_tags, "slabs": slabs, "items": items, "cut": cut,
            "source": ", ".join(x for x, on in (("slab callouts", slabs), ("printed room tags", tags),
                                               ("room labels", rooms), ("printed area table", table),
                                               ("printed equipment tags (piping plan)", piping)) if on)}


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
    # dataset's Estonian (Maleva 18), German / Swiss and Finnish room names. Labels + colours = the glance card's groups and
    # W7's 3D floor tints (web/src/look3d.ts ROOM_TYPES, main.ts GLANCE_HEX): ONE taxonomy on the plan, the card, the 3D
    # and F8's report (UX M5 23:30); bedrooms join living, plant joins storage, as there.
    ("wet", "Wet rooms", "#bfdcdc",
     r"toilet|wc\b|w\.c|shower|bath|washroom|wasch|lavator|hygiene|changing|laundry|sauna|saun\b|cleaner|urinal|en.?suite|"
     r"utility|wash|tualett|vannituba|dušš|pesuruum|pesukoda|koristus|bad\b|dusche|kph\b|kylpy|pesuhuone|pukuhuone|"
     r"siivous|baño|bano\b|aseo|kleedkamer|douche|sanitair|omklædning|omkladning|omklädning"),
    ("circulation", "Circulation", "#d9e2cf",
     r"corridor|circulation|lobby|stair|lift|elevator|landing|entrance|vestibule|foyer|walkway|enclosed\s+wal|passage|hallway|"
     r"ramp|porch|hall\s*way|entry\b|airlock|escape|koridor|esik\b|trepikoda|fuajee|tuulekoda|korridor|flur\b|"
     r"diele|treppenhaus|eingang|käytävä|eteinen|porras|tuulikaappi|aula\b|distribuidor|dist\b|rellano|pasillo|portal\b|"
     r"gang\b|hal\b|overloop|waiting|wait\b|vest\b"),
    ("living", "Bedrooms and living", "#e3d3b6",
     r"living|lounge|family|day\s*room|common|dayroom|elutuba|wohnen|wohnzimmer|zimmer|olohuone|ühistegevus|"
     r"room\s*\d|bed|dorm|sleep|magamis|schlaf|makuuhuone|habitaci|hab\b|dormitorio|salon|salón|estar\b|sal\b|"
     r"slaapkamer|woonkamer|sovrum|soverum|vardagsrum|stue\b"),
    ("kitchen", "Kitchen and dining", "#ecc9b0",
     r"kitchen|servery|food|pantry|cafe|canteen|dining|tea\b|freezer|chiller|köök|küche|keittiö|essen|cocina|coc\b|"
     r"keuken|kantine|kök\b|køkken"),
    ("teaching", "Teaching", "#e6d58f",
     r"classroom|class\b|group\s*room|teaching|english|maths?\b|science|lab|art\b|music|drama|ict\b|mfl\b|library|"
     r"learning|studio|swr\b|seminar|tutorial|resource|practical|nursery|sen\b|sensory|soft\s*play|quiet\s*room|"
     r"workshop|d\s*&\s*t\b|klass|arvutiklass|ringiruum|humanitaar|luokka"),
    ("hall", "Halls and sport", "#bccbe6",
     r"hall\b|gym|sports?\b|fitness|auditorium|assembly|multi.?purpose|theatre|saal\b|turnhalle|liikuntasali"),
    ("staff", "Staff and offices", "#e2c3d0",
     r"office|staff|admin|meeting|reception|interview|head|reprographic|workroom|senco|medical|sick|therapy|"
     r"parent|mi\s+room|conference|ncoic|nco\b|clinic|consult|copy|print|personal|töötaja|hooldus|valveruum|kabinet|"
     r"büro|toimisto|break\s*room|kontor|besprechung|conf\b|conference|work\s*room|cubicle"),
    ("storage", "Storage and plant", "#cfcdc8",
     r"store|storage|stock|cupboard|closet|cloak|locker|wardrobe|bin\b|refuse|abiruum|panipaik|ladu\b|liikumisvahend|"
     r"reduit|abstell|keller|lager|varasto|garaje|trastero|berging|kast\b|werkkast|meterkast|cv\s*ruimte|jan\b|"
     r"soil|trash|sto\b|stor\b|haz\b|teknikrum|comm\b|"
     r"plant\b|plant\s*room|riser|server|electrical|elec\b|mechanical|mech\b|boiler|comms|data\b|switch|meter|duct|"
     r"shaft|tank|sprinkler|generator|ups\b|telecom|janitor|sõlm|soojus|el\.?\s*kilp|kilp\b|ventilatsioon|"
     r"technik|heizung|tekninen|konehuone"),
)
OTHER_TYPE = ("other", "Other rooms", "#ebe9e5")
# Finnish (and Finland-Swedish) apartment-plan room CODES, printed alone or over the room's m² ('OH 16,3', 'MH 12,5',
# 'PESUH.', 'S'): CubiCasa-style plans and Finnish housing sheets name rooms only this way. A code is a room name only
# as the WHOLE label (an English sheet's 'K' or 'S' elsewhere is no room; '2H+K+S 55,0 m²' is the flat's type + total,
# not a room). Meaning in English for the row's tooltip.
FI_CODES = {
    "h": ("living", "room"), "mh": ("living", "bedroom"), "oh": ("living", "living room"),
    "ah": ("living", "living room"), "th": ("living", "study"), "lh": ("living", "children's room"),
    "k": ("kitchen", "kitchen"), "kk": ("kitchen", "kitchenette"), "kt": ("kitchen", "kitchen area"),
    "rt": ("kitchen", "dining area"), "ruok": ("kitchen", "dining area"), "ktk": ("kitchen", "kitchenette"),
    "kh": ("wet", "bathroom"), "kph": ("wet", "bathroom"), "ph": ("wet", "washroom"), "pesuh": ("wet", "washroom"),
    "s": ("wet", "sauna"), "l.h": ("wet", "sauna"), "wc": ("wet", "toilet"),
    "khh": ("wet", "utility room"), "sk": ("wet", "cleaner's cupboard"), "pkh": ("wet", "changing room"),
    "pukuh": ("wet", "changing room"), "suihku": ("wet", "shower"),
    "et": ("circulation", "entrance hall"), "tk": ("circulation", "vestibule"), "aula": ("circulation", "lobby"),
    "käyt": ("circulation", "corridor"), "kayt": ("circulation", "corridor"), "porr": ("circulation", "stairs"),
    "vh": ("storage", "walk-in closet"), "var": ("storage", "storage"), "hv": ("storage", "closet"),
    "kom": ("storage", "closet"), "tekn": ("storage", "technical room"), "lvi": ("storage", "plant room"),
    "iv": ("storage", "ventilation plant"), "iv-k": ("storage", "ventilation plant"),
    # Finland-Swedish
    "sov": ("living", "bedroom"), "vardagsrum": ("living", "living room"), "vr": ("living", "living room"),
    "kök": ("kitchen", "kitchen"), "bad": ("wet", "bathroom"), "badrum": ("wet", "bathroom"), "bastu": ("wet", "sauna"),
    "tvättstuga": ("wet", "laundry"), "tvr": ("wet", "laundry"), "tamb": ("circulation", "entrance hall"),
    "tambur": ("circulation", "entrance hall"), "förr": ("storage", "storage"), "förråd": ("storage", "storage"),
    "klk": ("storage", "walk-in closet"), "kl.k": ("storage", "walk-in closet"),
}
FI_OCR = {"hh": "mh", "0h": "oh", "nh": "mh", "wh": "mh", "xh": "kh", "et.": "et"}  # OCR misreads (X1-style scan: 'HH' = MH)
_FI_AREA = re.compile(r"\s*\(?\d{1,3}(?:[.,]\d{1,2})?\)?\s*(?:m2|m²|m\^2)?\s*$", re.I)


# codes no English / German sheet prints as a whole label (grid bubbles are single letters: H, K, S are grid lines there)
FI_DISTINCT = {"mh", "oh", "ah", "kk", "kt", "kh", "kph", "ph", "pesuh", "khh", "et", "vh", "ruok", "pkh", "pukuh",
               "sov", "tamb", "tambur", "klk", "förr", "förråd", "bastu", "kök"}


_FI_WORDS = re.compile(r"\b(olohuone|keittiö|keittio|makuuhuone|kylpyhuone|eteinen|vaatehuone|pesuhuone|kodinhoitohuone|"
                       r"tuulikaappi|varasto|kylmiö|parveke|työhuone|ruokailu|asuinhuone|lasitettu|keittokomero|"
                       r"vardagsrum|sovrum|badrum|förråd|tambur)", re.I)


def _is_fi_sheet(pdf: str, page: int) -> bool:
    try:
        from stoa.evidence.linework import text_lines

        return fi_sheet(t for t, _ in text_lines(pdf, page))
    except Exception:  # noqa: BLE001
        return False


def fi_sheet(texts) -> bool:
    """True when a sheet's labels hold >= 2 different distinctive Finnish room codes ('OH', 'MH', 'KH', 'ET'): only then
    do short codes ('K', 'S', 'H') count as rooms."""
    seen = set()
    for t in texts:
        w = _FI_WORDS.search(str(t))
        if w:
            seen.add(w.group(1).lower())
        if fi_code(t):
            k = _FI_AREA.sub("", " ".join(str(t).split()).lower()).strip().rstrip(".:")
            k = re.sub(r"\d+$", "", k)
            if k in FI_DISTINCT:
                seen.add(k)
    return len(seen) >= 2


def fi_code(text: str) -> tuple[str, str] | None:
    """('living', 'bedroom') for 'MH' / 'MH 12,5' / 'MH2' / 'mh.'; None for anything that is not a whole room code."""
    t = " ".join(str(text or "").split()).strip().lower()
    for _ in range(2):  # 'MH2 12,5' -> 'MH2' -> 'MH'
        t2 = _FI_AREA.sub("", t).strip()
        if not t2:
            break
        t = t2
    t = t.rstrip(".:").strip()
    if not t or len(t) > 11 or not re.fullmatch(r"[a-zåäö0.\-]+", t):
        return None
    t = FI_OCR.get(t, t)
    return FI_CODES.get(t) or FI_CODES.get(t.replace(".", ""))


def fi_name(text: str) -> str | None:
    """The printed code, spelt as the sheet means it ('HH' read by OCR -> 'MH'; 'OH 16,3' -> 'OH'), or None."""
    if not fi_code(text):
        return None
    t = _FI_AREA.sub("", " ".join(str(text).split())).strip()
    t = _FI_AREA.sub("", t).strip().rstrip(".:") or t
    k = FI_OCR.get(t.lower(), t.lower())
    return k.upper() if k != t.lower() else t
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
    fi = fi_code(text)  # a Finnish apartment plan's whole-label room code ('OH', 'MH 12,5', 'PESUH')
    if fi:
        return fi[0]
    # an English room name ends in its head noun: 'Sports Store' is a store, 'Reception Classroom' a classroom, 'Dining
    # Hall' a hall (the first type word used to win: SEC L00 'Sports Equipment Store' = hall). Read on the name before
    # '(' and before a '+' / '/' / '&' / 'and' (ELUTUBA+KÖÖK = a living room with a kitchen); a wet word anywhere keeps
    # the room wet (tiled: its skirting flag).
    if _TYPE_RX[0][1].search(text):
        return _TYPE_RX[0][0]
    head = re.split(r"\s*(?:[+/&]|\band\b)\s*", text.split("(")[0].strip() or text, maxsplit=1)[0] or text
    last = {k: max(m.start() for m in ms) for k, rx in _TYPE_RX if (ms := list(rx.finditer(head)))}
    if last:
        top = max(last.values())
        return next(k for k, _ in _TYPE_RX if last.get(k) == top)
    for k, rx in _TYPE_RX:
        if rx.search(text):
            return k
    # German / Swiss names typed without umlauts ('Buero 2', 'Kueche', 'Waschkueche': F8 23:57, fzk_house_arc.ifc)
    de = re.sub(r"(?i)([aou])e", lambda m: {"a": "ä", "o": "ö", "u": "ü", "A": "Ä", "O": "Ö", "U": "Ü"}[m.group(1)], text)
    if de != text:
        k = room_type(de)
        if k != OTHER_TYPE[0]:
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
        return f"rough reading (confidence {c:.2f})" + (f": {row['rough']}" if row.get("rough") else "")
    if q == "baseboard":
        per = _perimeter(row)
        if per and v < 0.3 * per:
            return f"only {v / per:.0%} of the {per:.1f} m outline counted as wall: check the openings"
    ow = row.get("off_wall")
    if q == "area" and isinstance(ow, (int, float)) and ow >= OFF_WALL_REVIEW:
        return (f"its outline runs along drawn walls for only {1 - ow:.0%} of its length (leaked through a gap, or caught "
                f"furniture): check it or Measure it")
    return None


OFFWALL_PICTURES = os.environ.get("STOA_TAKEOFF_OFFWALL_PICTURES", "1") == "1"  # traced copies too: DfE PNG max 0.66, its
#  gold-matching rooms 0.06-0.15 (nothing flagged); 0 = vector PDFs only
OFF_WALL_REVIEW = 0.8  # share of a room outline with no drawn line under it: unseen DfE Secondary L00 'Art Room (multi-
#  purpose)' 327.7 m² leaked through a glazed facade (0.88) and three 'Classrooms' of 6.7-11.4 m² caught furniture
#  (0.82-0.91); the catalogue's right rooms stay <= 0.75 (SEN L00 Lobby), most < 0.4


def off_wall(pdf: str, page: int, n: float | None, poly: list) -> float | None:
    """The share of a room outline (pdf_pt) that runs over blank paper (no ink within 0.10 m or 2.5 px): a traced room
    follows its walls except at door ways and open sides. None when the page or scale cannot be read."""
    try:
        import numpy as np
        from shapely.geometry import Polygon

        from stoa.evidence import linework as L

        pg, d = L._page(pdf, page), L.ink_dist(pdf, page)
        if pg is None or d is None or not n or not poly or len(poly) < 3:
            return None
        ppt = pg["ppt"]
        H, W = d.shape
        tol = max(2.5, 0.10 / (n * L.PT_TO_M_AT_1) * ppt)
        ring = Polygon([tuple(p) for p in poly]).exterior
        k = max(16, min(4000, int(ring.length * ppt)))
        free = 0
        for s_ in np.linspace(0.0, ring.length, k, endpoint=False):
            q = ring.interpolate(float(s_))
            x, y = int(round(q.x * ppt)), int(round(q.y * ppt))
            free += (float(d[y, x]) if 0 <= x < W and 0 <= y < H else 255.0) > tol
        return round(free / k, 3)
    except Exception:  # noqa: BLE001
        return None


SAME_OUTLINE = os.environ.get("STOA_TAKEOFF_SAME_OUTLINE", "1") != "0"


def _same_outline(rows: list[dict]) -> None:
    """Two labels traced to ONE outline are one floor area, counted once. The demo's DfE L00 PNG: OCR read 'Enclosed
    Walkway' as two labels, 'Enclosed Wall' and 'Walkway', both traced the same 119.39 m² outline -> the net floor area
    said 1,736.96 m² (the PDF twin: 1,613.2 with one 'Enclosed Walkway' 118.98). The same holds for two real labels in
    one open space (the tracer found no wall between them). The first label (surest, not a wall word, then sheet order)
    keeps the numbers; every row of the other goes to Review 'same outline as …', so it is visible but not summed.
    Match = same page, an area within 0.5 % and an evidence box within 1 % of its size (a traced polygon repeated)."""
    if not SAME_OUTLINE:
        return

    def key(r: dict):
        ev = next((e for e in r.get("evidence") or [] if e.get("bbox")), None)
        v = r.get("value")
        if ev is None or not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0 or r.get("fixed"):
            return None
        b = [float(x) for x in ev["bbox"][:4]]
        if len(b) < 4 or b[2] - b[0] < 2 or b[3] - b[1] < 2:  # a placeholder box locates nothing
            return None
        return ev.get("page", r.get("page")), b, float(v)

    area = [(i, r, key(r)) for i, r in enumerate(rows)
            if r.get("section") == "room" and r.get("quantity") == "area"]
    area = [(i, r, k) for i, r, k in area if k is not None]
    rank = sorted(area, key=lambda t: (-float(t[1].get("confidence") or 0),
                                       bool(re.search(r"\bwalls?\b", str(t[1].get("item") or ""), re.I)), t[0]))
    kept: list[tuple[dict, tuple]] = []
    dup: dict[tuple, dict] = {}
    for _, r, k in rank:
        pg, b, v = k
        tol = 0.01 * max(b[2] - b[0], b[3] - b[1], 1e-9)
        hit = next((kr for kr, (kpg, kb, kv) in kept if kpg == pg and abs(kv - v) <= 0.005 * max(kv, v)
                    and all(abs(x - y) <= tol for x, y in zip(kb, b))), None)
        if hit is None:
            kept.append((r, k))
        else:
            dup[(r.get("item"), r.get("copy", 1))] = hit
    if not dup:
        return
    for r in rows:
        hit = dup.get((r.get("item"), r.get("copy", 1))) if r.get("section") == "room" else None
        if hit is not None:
            n = f" ({hit.get('copy', 1)}/{hit.get('copies')})" if (hit.get("copies") or 1) > 1 else ""
            what = "outline" if any(e.get("polygon") for e in hit.get("evidence") or []) else "reading"
            u = {"m2": "m²", "ft2": "ft²"}.get(str(hit.get("unit") or "m2"), str(hit.get("unit")))
            note = (f"same {what} as {hit.get('item')}{n} ({hit.get('value'):g} {u}): counted once, "
                    f"there; check it (two labels in one space, or this room's own walls were not found)")
            r["same_outline"] = hit.get("id")
            r["review"] = f"{r['review']}; {note}" if r.get("review") else note


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


_OPEN_RX = re.compile(r"opening (\d+(?:\.\d+)?) m")
_PER_RX = re.compile(r"(?:perimeter|outline) (\d+(?:\.\d+)?) m")


def components(rows: list[dict], st: dict) -> None:
    """Per room, the parts a convention switch needs (captain 21:51, for F4 net / centre line / gross, F3 orders, F6
    checks, F5 costs), set as `components` on EVERY row of the room (same dict): room_type, wet, area_m2,
    perimeter_m (inner wall-face outline = skirting before openings), skirting_net_m, openings_m (each opening's width,
    jamb to jamb), doors (sets) + door_leaves + leaves_per_door, windows, wall_thickness_m (None: not measured),
    confidence (the lowest of the room's readings), agree (True / False when a second method checked the area: the IFC
    space or the traced outline vs a printed tag; None = one method), methods."""
    rooms: dict = {}
    for r in rows:
        if r.get("section") == "room":
            rooms.setdefault((r.get("item"), r.get("copy", 1)), {})[r.get("quantity")] = r
    n = st.get("scale_n")
    for (item, copy), d in rooms.items():
        a, b, dr, w = d.get("area"), d.get("baseboard"), d.get("doors"), d.get("windows")
        num = lambda r: r.get("value") if r and isinstance(r.get("value"), (int, float)) else None  # noqa: E731
        per, opens = None, None
        if b:
            parts = b.get("parts") or {}
            per = parts.get("perimeter_m")
            opens = parts.get("openings_m")
            texts = [*(b.get("assumptions") or []), *[e.get("note") or "" for e in b.get("evidence") or []]]
            if per is None:
                m = next((_PER_RX.search(t) for t in texts if _PER_RX.search(t)), None)
                per = float(m.group(1)) if m else None
            if opens is None:
                found = [float(x) for t in texts for x in _OPEN_RX.findall(t)]
                opens = found or None
        if per is None and a and n and str(st.get("kind", "")).startswith("vector"):
            poly = next((e.get("polygon") for e in a.get("evidence") or [] if e.get("polygon")), None)
            if poly and len(poly) > 2:
                pts = poly + [poly[0]]
                per = round(sum(((pts[i + 1][0] - pts[i][0]) ** 2 + (pts[i + 1][1] - pts[i][1]) ** 2) ** 0.5
                                for i in range(len(pts) - 1)) * 0.0254 / 72 * float(n), 3)
        lpd = None
        if dr and isinstance(dr.get("value"), (int, float)) and len(dr.get("evidence") or []) == int(dr["value"]):
            lpd = [2 if re.search(r"2 leaves|double door|double swing|\bpair\b", e.get("note") or "", re.I) and
                   "double-acting" not in (e.get("note") or "") else 1 for e in dr.get("evidence") or []]
        chk = (a or {}).get("check")
        confs = [float(r.get("confidence") or 0) for r in d.values() if num(r) is not None]
        comp = {"room_type": (a or b or {}).get("room_type"), "wet": bool((a or b or {}).get("wet")),
                "area_m2": num(a) if (a or {}).get("unit", "m2") == "m2" else None,
                "perimeter_m": per, "skirting_net_m": num(b), "openings_m": opens,
                "doors": num(dr), "door_leaves": (dr or {}).get("leaves"), "leaves_per_door": lpd,
                "windows": num(w), "wall_thickness_m": None,
                "confidence": round(min(confs), 3) if confs else None,
                "agree": chk.get("agree") if chk else None,
                "methods": sorted({r.get("method") or "" for r in d.values() if r.get("method")})}
        for r in d.values():
            r["components"] = comp


def totals(rows: list[dict]) -> list[dict]:
    """Derived rows from the finished rows: rooms labelled, NET floor area, skirting total (with and without wet
    rooms), door sets / leaves. Every sum leaves the review rows out and says how many it used."""
    out = []
    area = [r for r in rows if r.get("section") == "room" and r.get("quantity") == "area"]
    if area and all(r.get("ifc_guid") for r in area):  # a building model: its IfcSpaces, not printed labels
        out.append({"section": "total", "item": "Rooms (IfcSpaces)", "quantity": "rooms", "unit": "count",
                    "value": len(area), "confidence": 0.95, "status": "ok", "method": "the model's IfcSpace elements",
                    "assumptions": [f"{len({r.get('storey') for r in area})} storeys"], "evidence": [], "seconds": 0})
    elif area:
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
        # coverage, so a total of a few rooms is not read as the floor's (UX 02:05: '3 rooms · 22 m' on a 25-room floor)
        n_rooms = len({(r.get("item"), r.get("copy", 1)) for r in area}) or len(base)
        if len(base) < n_rooms:
            rule += (f"; {len(base)} of {n_rooms} rooms ({n_rooms - len(base)} have no skirting reading or are in "
                     f"Review: not in this total)")
        out.append(_sum_row("Skirting total", "baseboard_sum", "m", base, rule,
                            [f"includes {len(wet)} wet rooms ({sum(r['value'] for r in wet):.1f} m): " + WET_NOTE]
                            if wet else []))
        dry = [r for r in base if not r.get("wet")]
        if wet and dry:
            out.append(_sum_row("Skirting total without wet rooms", "baseboard_dry", "m", dry,
                                rule + "; wet rooms (toilets, showers, changing, laundry: usually tiled) left out"))
    sd = next((r for r in rows if r.get("section") == "sheet" and r.get("quantity") == "doors"
               and isinstance(r.get("value"), (int, float))), None)
    if sd is not None and sd.get("leaves") is not None and not sd.get("review"):
        out.append({"section": "total", "item": "Door leaves (whole sheet)", "quantity": "door_leaves",
                    "unit": "count", "value": sd["leaves"], "confidence": sd.get("confidence"), "status": "ok",
                    "method": sd.get("method", ""), "evidence": [], "seconds": 0, "of": [sd.get("id")],
                    "assumptions": [f"{sd['value']} door sets ({sd['leaves'] - sd['value']} of them double: 2 "
                                    "leaves each)"]})
    return out


def finish(st: dict, virtual: str | None = None, abs_path: str | None = None, fixes: dict | None = None) -> dict:
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
    _reown_windows(st, rows)
    if (st.get("traced") or "traced" in str(st.get("kind") or "")) and os.environ.get("STOA_TAKEOFF_OCR_SNAP", "1") != "0":
        snap = {}
        for r in rows:  # a scan's / photo's OCR names snapped to room words; what the OCR read is kept beside it
            if r.get("section") == "room" and r.get("item") and r["item"] not in snap:
                snap[r["item"]] = ocr_snap(r["item"])
        if any(k != v for k, v in snap.items()):
            # rooms that now share a name are numbered again (copy i of N), the ones printed that way first: else
            # 'Seoundary Classmom' 1/1 would land on 'Secondary Classroom' 1/6 (one line for two rooms)
            keys: dict = {}
            for r in rows:
                if r.get("section") == "room" and r.get("item"):
                    keys.setdefault(snap[r["item"]], {})[(r["item"], r.get("copy", 1))] = None
            new = {}
            for name, olds in keys.items():
                for i, old in enumerate(sorted(olds, key=lambda o: (o[0] != name, o[0], o[1])), 1):
                    new[old] = (name, i, len(olds))
            for r in rows:
                if r.get("section") == "room" and r.get("item"):
                    name, i, n = new[(r["item"], r.get("copy", 1))]
                    if name != r["item"]:
                        r["ocr_name"] = r["item"]
                    r["item"], r["copy"], r["copies"] = name, i, n
    if "sheet" not in st or re.fullmatch(r"[A-Z]\d{2}", str(st.get("sheet") or "")):  # older runs stored 'C01' (a revision)
        ap = abs_path or st.get("abs")
        if not ap and st.get("file"):
            try:
                ap = resolve(str(st["file"]))[0]
            except Exception:  # noqa: BLE001
                ap = None
        sh, rv = sheet_rev(ap, st.get("page") or 0)
        st["sheet"], st["revision"] = sh or ("" if "sheet" in st else sh), rv or st.get("revision")
    for i, r in enumerate(rows):
        r["id"] = f"r{i}"
        fx = (fixes or {}).get(r["id"])
        if fx and fx.get("derived_from") and isinstance(fx.get("value"), (int, float)) \
                and fx.get("item", r.get("item")) == r.get("item") and r.get("quantity") == "baseboard":
            # the skirting of the outline a person measured for this room's area (fix(): perimeter minus the openings
            # found along THEIR outline)
            ops = fx.get("openings") or []
            r["reading"] = {"value": r.get("value"), "confidence": r.get("confidence"), "method": r.get("method")}
            sp = fx.get("space") or "pdf_pt"
            r.update(value=round(float(fx["value"]), 2), unit="m", confidence=0.8, status="ok", fixed=fx.get("at"),
                     derived=True,
                     method=(f"from your measured outline: perimeter {fx.get('perimeter_m'):g} m minus {len(ops)} "
                             f"opening{'s' if len(ops) != 1 else ''} "
                             f"{sum(float(o.get('m') or 0) for o in ops):.2f} m (Measure tool)"),
                     evidence=[{"file": virtual or st.get("file"), "page": st.get("page"), "bbox": _union(
                         [[min(x for x, _ in fx["polygon"]), min(y for _, y in fx["polygon"]),
                           max(x for x, _ in fx["polygon"]), max(y for _, y in fx["polygon"])]]),
                         "bbox_space": sp, "polygon": fx["polygon"],
                         "note": f"your outline: skirting {fx['value']:g} m = perimeter {fx.get('perimeter_m'):g} m "
                                 f"minus the openings"}]
                     + [{"file": virtual or st.get("file"), "page": st.get("page"), "bbox_space": sp,
                         "bbox": _union([[min(o["a"][0], o["b"][0]), min(o["a"][1], o["b"][1]),
                                          max(o["a"][0], o["b"][0]), max(o["a"][1], o["b"][1])]]),
                         "polyline": [o["a"], o["b"]], "note": f"opening {float(o.get('m') or 0):.2f} m (no skirting)"}
                        for o in ops if o.get("a") and o.get("b")][:20])
        elif fx and isinstance(fx.get("value"), (int, float)) and fx.get("item", r.get("item")) == r.get("item"):
            # measured by a person on the drawing (the panel's 'Measure' on a Review row): their number counts
            r["reading"] = {"value": r.get("value"), "confidence": r.get("confidence"), "method": r.get("method")}
            r.pop("off_wall", None)  # the person's outline replaces the one that ran off the walls
            r.update(value=round(float(fx["value"]), 2), unit=fx.get("unit") or r.get("unit"), confidence=0.95,
                     status="ok", method="measured on the drawing by a user (Measure tool)", fixed=fx.get("at"),
                     evidence=([{"file": virtual or st.get("file"), "page": st.get("page"), "bbox": _union(
                         [[min(x for x, _ in fx["polygon"]), min(y for _, y in fx["polygon"]),
                           max(x for x, _ in fx["polygon"]), max(y for _, y in fx["polygon"])]]),
                         "bbox_space": fx.get("space") or "pdf_pt", "polygon": fx["polygon"],
                         "note": f"measured by a user: {fx['value']:g} {fx.get('unit') or ''}"}]
                         if fx.get("polygon") else []) + list(r.get("evidence") or [])[:1])
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
    # labels that are no room ('Enclosed Wall', canopy, site): run() drops them since 22:11, but a job stored before that
    # (the catalogue's warmed caches) still holds them -> dropped at read time too; after the ids, so fixes stay put
    rows = [r for r in rows if not (r.get("section") == "room" and _OUTSIDE_RX.search(_clean(r.get("item") or "")))]
    _same_outline(rows)
    # a room's area / skirting answered IN COUNT is another quantity (UX 02:05, the demo's Swiss floor 9385: 'How many
    # linear metres of baseboard does the Whg. B Zimmer need' -> pipeline/dxf '8' = a door count, evidence = door marks;
    # 'the router read this as a length question; dxf answered in count'): no reading, its marks dropped, the count
    # kept in the note only. At read time, so the jobs cached before this rule are cleaned too.
    for r in rows:
        if (r.get("section") == "room" and r.get("quantity") in ("baseboard", "area") and not r.get("fixed")
                and str(r.get("unit") or "").lower() == "count"
                and isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool)):
            what = "a length" if r["quantity"] == "baseboard" else "an area"
            r.update({"note": f"the reader answered a count ({r['value']:g}), not {what}: no reading for this room",
                      "value": None, "confidence": 0.0, "status": "abstain", "evidence": [], "review": None,
                      "unit": "m" if r["quantity"] == "baseboard" else "m2"})
    # skirting that cannot belong to a room of that size (Swiss sheet: a reader answered '1 m' for an 18.36 m² room):
    # a square room of area A has 4·sqrt(A) m of wall; under 35 % of that or over 4x is not a skirting length
    areas = {(r.get("item"), r.get("copy", 1)): r.get("value") for r in rows
             if r.get("quantity") == "area" and r.get("section") == "room" and isinstance(r.get("value"), (int, float))}
    for r in rows:
        if r.get("quantity") == "baseboard" and isinstance(r.get("value"), (int, float)) and not r.get("review"):
            a = areas.get((r.get("item"), r.get("copy", 1)))
            if a and a > 1:
                sq = 4 * a ** 0.5
                if r["value"] < 0.35 * sq or r["value"] > 4 * sq:
                    r["review"] = (f"{r['value']:g} m of skirting does not fit a {a:g} m² room (a square room that "
                                   f"size has {sq:.1f} m of wall): check it")
    # cross-check of two readings of one room: no door swing on its outline, but the skirting rule found openings in
    # its walls (QA 20:45 'Fitness Room' 0 on the DfE PNG: the vector sheet has 1; its arc was lost in the tracing)
    gaps = {}
    for r in rows:
        if r.get("quantity") == "baseboard":
            m = next((re.search(r"minus (\d+) opening", str(a)) for a in r.get("assumptions") or []
                      if re.search(r"minus (\d+) opening", str(a))), None)
            if m:
                gaps[(r.get("item"), r.get("copy", 1))] = int(m.group(1))
    # only where arcs can be LOST (a traced picture / scan): on a vector sheet every drawn door is read exactly (swings,
    # leaves, folding / sliding symbols), so an opening with none is an open doorway (F6 23:23: Esplanades living +
    # kitchen rooms open from the hall, the model agrees with 0 doors on 17 of 19)
    traced = bool(st.get("traced")) or "traced" in str(st.get("kind") or "")
    for r in rows:
        if r.get("quantity") == "doors" and r.get("section") == "room" and r.get("value") == 0 and not r.get("review"):
            n = gaps.get((r.get("item"), r.get("copy", 1)), 0)
            if n and traced:
                r["review"] = (f"no door swing found on its outline, but its walls have {n} opening"
                               f"{'s' if n > 1 else ''}: a door may be drawn without an arc")
            elif n:
                r.setdefault("assumptions", []).append(f"{n} opening{'s' if n > 1 else ''} in its walls with no door drawn "
                                                 "(open doorway)")
    # a sheet door count no building has. With labelled rooms: more than DOORS_PER_ROOM_MAX per room (DfE L00 1.1, its
    # scan 1.5 with a third of the labels unread, Maleva 0.8, Swiss 1.4). With none: more than one door per DOOR_M2_MIN
    # m² of the printed floor total (AFCEC A-2 PNG traced: 494 'doors' on 2,468 m² = one per 5 m²; its PDF 81 = 30 m²)
    n_rooms = len({(r.get("item"), r.get("copy", 1)) for r in rows if r.get("section") == "room"})
    floor = _floor_m2(rows) if n_rooms < 5 else None
    for r in rows:
        if not (r.get("section") == "sheet" and r.get("quantity") == "doors" and isinstance(r.get("value"), (int, float))
                and r["value"] > 0 and not r.get("review")):
            continue
        if n_rooms >= 5 and r["value"] > DOORS_PER_ROOM_MAX * n_rooms:
            r["review"] = (f"{r['value']:g} doors for {n_rooms} labelled rooms: more than {DOORS_PER_ROOM_MAX:g} per room "
                           f"(buildings have about 1-2); check the marks")
        elif floor and r["value"] * DOOR_M2_MIN > floor:
            r["review"] = (f"{r['value']:g} doors on a {floor:,.0f} m² floor = one per {floor / r['value']:.1f} m²: more "
                           f"than buildings have (usually one per 15-40 m²); check the marks")
    if st.get("traced") and n_rooms == 0:  # AFCEC A-2 PNG: 33 windows traced vs 62 on its PDF twin, 48 dorm rooms
        for r in rows:
            if (r.get("section") == "sheet" and r.get("quantity") in ("doors", "windows") and not r.get("review")
                    and isinstance(r.get("value"), (int, float))):
                r["review"] = ("counted on lines traced from a picture of the sheet, with no room outlines on it to check "
                               "the count against: check the marks")
    components(rows, st)
    rows.extend(totals(rows))
    st["rows"] = rows
    st["review_count"] = sum(1 for r in rows if r.get("review"))
    return st


DOOR_M2_MIN = 8.0  # m² of floor per door below which a sheet's door count goes to Review (sheets without rooms)
DOORS_PER_ROOM_MAX = 4.0  # doors per labelled room above which it goes to Review
FT2_M2 = 0.09290304


def _floor_m2(rows: list[dict]) -> float | None:
    """The sheet's floor area in m²: the sum of its rooms (outside Review), else its printed floor total."""
    to_m2 = lambda r: float(r["value"]) * (FT2_M2 if str(r.get("unit")).startswith("ft") else 1.0)  # noqa: E731
    rooms = [to_m2(r) for r in rows if r.get("section") == "room" and r.get("quantity") == "area"
             and isinstance(r.get("value"), (int, float)) and not r.get("review")]
    if rooms:
        return sum(rooms)
    tots = [to_m2(r) for r in rows if r.get("section") == "printed" and r.get("quantity") == "area"
            and isinstance(r.get("value"), (int, float)) and _TOTAL_RX.search(str(r.get("item") or ""))]
    return max(tots) if tots else None


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


_ZONE_RX = re.compile(r"^(?:(?:north|south|east|west|central|main|upper|lower|left|right|wing|block|zone|level|floor|"
                      r"building|[a-h])\b\s*[-:]?\s*)+", re.I)
_SPLIT_TYPES = ("circulation", "corridor", "utility", "storage", "mechanical", "toilets?", "restrooms?")
_TOTAL_RX = re.compile(r"\b(?:sub\s*-?\s*total|total|sum|gross)\b", re.I)


NAME_FUZZ = 0.85  # difflib ratio at which a rarer room name on the sheet is an OCR misspelling of a frequent one


def _marks(name: str) -> list[str]:
    """The numbers and single letters of a room name (room / flat numbers, unit letters): names that differ in them are
    different rooms however close their letters are."""
    return sorted(t.lower() for t in re.findall(r"[^\W\d_]+|\d+", name)
                  if t.isdigit() or len(t) == 1 or t.lower() in _QUALIFIERS)


# words that make two otherwise equal names different rooms (EFA: 'Male WC' / 'Female WC' are 0.875 apart by letters)
# (only opposite pairs: a qualifier OCR may garble, 'Socondary', must not block the merge it is there for)
_QUALIFIERS = {"male", "female", "men", "women", "mens", "womens", "boys", "girls", "ladies", "gents", "north", "south",
               "east", "west", "upper", "lower", "inner", "outer", "left", "right", "front", "back"}


_WORDS = None


def _spelled(name: str) -> float:
    """The share of a name's words (3+ letters) that are real words (the system's word list, the room types' words):
    'Accessible Toilet' 1.0, OCR's 'Accessitie Toilet' 0.5. Names in a language the list lacks score 0 alike."""
    global _WORDS
    if _WORDS is None:
        _WORDS = set()
        for f in ("/usr/share/dict/words", "/usr/share/dict/british-english", "/usr/share/dict/american-english"):
            try:
                _WORDS |= {w.strip().lower() for w in open(f, encoding="utf-8", errors="ignore") if w.strip()}
            except OSError:
                continue
        room_type("")  # fills _VOCAB
        _WORDS |= set(_VOCAB or {})
    ws = [w.lower() for w in re.findall(r"[^\W\d_]{3,}", str(name or ""))]
    return sum(w in _WORDS for w in ws) / len(ws) if ws else 0.0


# words room names are made of (the scan's OCR is snapped to these): the room types' own words + common name words
_ROOM_WORDS = """room rooms store stores storage office offices staff group hall halls classroom classrooms toilet toilets
corridor kitchen plant lobby stair stairs lift changing cleaner cleaners accessible hygiene sensory therapy medical meeting
reception entrance secondary primary main sports fitness dining servery laundry shower showers studio music science
laboratory library workroom head teacher teaching enclosed wall area space riser server comms locker lockers cloakroom
interview soft play food practical design technology multi purpose equipment resource resources base small large void
above below disabled male female unisex boys girls pupil pupils visitor visitors waiting admin plantroom electrical
switch boiler tank vestibule landing circulation lounge common family living kitchenette pantry chiller freezer
workshop seminar nursery quiet calm social care deputy business manager bursar conference changing wc wcs cupboard
cupboards suite mens womens ladies gents utility preparation assembly gymnasium auditorium sick bay drama dance english
maths history geography faculty department bedroom bathroom ensuite garage hallway study dormitory""".split()


def ocr_snap(name: str) -> str:
    """A room name the OCR garbled on a scan / photo -> the nearest room words ('Croup Roo.m' -> 'Group Room',
    'Encloued Wall' -> 'Enclosed Wall', 'Saconcary Classro:m' -> 'Secondary Classroom'; F6 00:07). Only a name with a
    word no dictionary knows is touched; then each word not in the room vocabulary is replaced by the closest room word
    at difflib >= 0.8 (stray '.' / ':' inside a word dropped first). Unsure words stay as read ('Staff Cmice')."""
    import difflib

    text = str(name or "")
    _spelled("")  # fills _WORDS
    toks = text.split(" ")
    clean = [re.sub(r"(?<=[A-Za-z])[.:,'`](?=[A-Za-z])", "", t) for t in toks]
    words = [re.sub(r"[^A-Za-z]", "", t).lower() for t in clean]
    if clean == toks and not any(len(w) >= 3 and w not in (_WORDS or ()) and w not in _ROOM_WORDS for w in words):
        return text  # every word is a real word and none had a stray mark inside: as printed
    vocab = sorted(set(_ROOM_WORDS))  # English words only: 'TUALETTRUUM' must not become the wet stem 'TUALETT'
    out = []
    for t, w in zip(clean, words):
        if (len(w) < 4 or w in _ROOM_WORDS or not re.fullmatch(r"[A-Za-z]+[).,:]?", t)
                or any(len(v) >= 4 and w.startswith(v) for v in vocab)):  # a compound / plural ('Storerooms')
            out.append(t)
            continue
        hit = difflib.get_close_matches(w, vocab, n=1, cutoff=0.75)
        if not hit:
            out.append(t)
            continue
        h = hit[0]
        out.append(h.upper() if t.isupper() and len(t) > 1 else h.capitalize() if t[:1].isupper() else h)
    return " ".join(out)


def by_name(rows: list[dict]) -> list[dict]:
    """Rooms printed with the same name more than once (DfE L00: 'Secondary Classroom' x5): the area and skirting summed
    over the copies outside Review (X1: 'total floor area of all Secondary Classrooms'; an estimator's typical room),
    with how many copies each sum used."""
    names: dict = {}
    for r in rows:
        if r.get("section") == "room" and r.get("quantity") in ("area", "baseboard"):
            # 'Secondary Classroom (Severe Needs)' is a Secondary Classroom too (X1: 8 = 6 + 2 '(Severe …)' labels); the
            # OCR / stacking may cut the qualifier ('… (Severe'): the base name is the text before the first '('
            base = _clean(str(r.get("item") or "").split("(")[0]) or str(r.get("item"))
            names.setdefault(base, []).append(r)
    # OCR on photos / scans garbles a few copies ('Socondary Classrcom'): a rarer spelling within NAME_FUZZ of a more
    # frequent name on the same sheet is that name (listed under 'names', so the merge stays visible)
    import difflib

    def ncopies(rs):
        return len({(r.get("item"), r.get("copy", 1)) for r in rs})

    count = {n: ncopies(rs) for n, rs in names.items()}
    spelled = {n: _spelled(n) for n in names}
    freq = sorted(names, key=lambda n: (-count[n], -spelled[n]))
    spellings = {n: [n] for n in names}  # the base names each group joined
    for i, rare in enumerate(reversed(freq)):
        if rare not in names:
            continue
        for main in freq:
            if main == rare or main not in names or len(names[main]) < len(names[rare]):
                continue
            # never across room numbers / unit letters ('KORIDOR 402' vs '405', Swiss 'Whg. B' vs 'Whg. C') or room types
            # ('Parent Room' is no misspelt 'Plant Room': 0.86 by letters, but another type)
            if (_marks(rare) == _marks(main) and room_type(rare) == room_type(main)
                    and difflib.SequenceMatcher(None, rare.lower(), main.lower()).ratio() >= NAME_FUZZ):
                names[main].extend(names.pop(rare))
                spellings[main].extend(spellings.pop(rare))
                break
    out = []
    for key, rs in names.items():
        copies = ncopies(rs)
        if copies < 2:
            continue
        # the group's label: the best-spelt of the names it joined, then the most frequent ('Accessible Toilet', not
        # OCR's 'Accessitie Toilet' even when the garbled copy came first)
        item = max(spellings[key], key=lambda n: (round(spelled[n], 2), count[n], -freq.index(n)))
        d = {"item": item, "copies": copies, "room_type": rs[0].get("room_type"),
             "names": sorted({str(r.get("item")) for r in rs})}
        for q in ("area", "baseboard"):
            ok = [r for r in rs if r.get("quantity") == q and isinstance(r.get("value"), (int, float)) and not r.get("review")]
            if ok:
                d[q] = {"value": round(sum(float(r["value"]) for r in ok), 2), "unit": ok[0].get("unit"), "used": len(ok),
                        "rows": [r.get("id") for r in ok], "confidence": round(min(float(r.get("confidence") or 0)
                                                                                   for r in ok), 3)}
        if "area" in d or "baseboard" in d:
            out.append(d)
    return sorted(out, key=lambda d: -((d.get("area") or {}).get("value") or 0))


def sum_rows(st: dict) -> list[dict]:
    """The summed lines of a finished takeoff, for the exports (the panel shows the same): rooms with one name
    (section 'same_name') and a printed area table by type (section 'printed_type'). Never part of any total."""
    rows = st.get("rows") or []
    by_id = {r.get("id"): r for r in rows if r.get("id")}
    base = {"sheet": st.get("sheet"), "revision": st.get("revision"), "page": st.get("page"), "status": "ok"}

    def ev(ids):
        return [e for i in ids for e in (by_id.get(i) or {}).get("evidence") or []][:60]

    out = []
    for n in by_name(rows):
        a = n.get("area")
        if not a:
            continue
        also = [x for x in n["names"] if x != n["item"]]
        out.append({**base, "section": "same_name", "item": f"{n['item']} x{n['copies']}", "quantity": "area",
                    "value": a["value"], "unit": a["unit"], "confidence": a["confidence"], "evidence": ev(a["rows"]),
                    "method": f"{a['used']} of {n['copies']} rooms summed" + (f"; incl. {', '.join(also)}" if also else "")})
    for x in st.get("storeys") or []:  # a building model, floor by floor
        ev_ = [{"file": st.get("file"), "page": None, "ifc_guid": g, "note": f"on {x['storey']}"}
               for g in (x.get("door_guids") or [])[:60]]
        for q, v, u in (("area", x["area"], "m2"), ("doors", x["doors"], "count"), ("windows", x["windows"], "count")):
            out.append({**base, "section": "storey", "item": x["storey"], "quantity": q, "value": v, "unit": u,
                        "confidence": 0.95, "evidence": ev_ if q == "doors" else [],
                        "method": (f"sum of the {x['rooms']} rooms on this storey" if q == "area" else
                                   f"IFC model: every {'IfcDoor' if q == 'doors' else 'IfcWindow'} on this storey")})
    pbt = printed_by_type(rows)
    for t in (pbt or {}).get("types") or []:
        c = pbt.get("check") or {}
        out.append({**base, "section": "printed_type", "item": t["type"].capitalize(), "quantity": "area",
                    "value": t["value"], "unit": t["unit"], "evidence": ev(t["rows"]),
                    "confidence": min([float((by_id.get(i) or {}).get("confidence") or 0.9) for i in t["rows"]] or [0.9]),
                    "method": "sum of " + " + ".join(t["items"]) + (f"; the types add up to {c.get('printed')} "
                                                                     f"{'✓' if c.get('agree') else '(differs)'}" if c else "")})
    return out


def printed_by_type(rows: list[dict]) -> dict | None:
    """A sheet's printed area table grouped by space TYPE (Stoa: 'areas by type', e.g. AFCEC A-2 'WEST WING DORMITORY',
    'EAST WING CIRCULATION', 'COMMON SPACE CIRCULATION' -> dormitory / circulation / common space), each type the sum
    of its printed rows, and checked against the table's own SUBTOTAL / TOTAL (types adding up to a printed total = the
    table was read whole). None unless two rows share a type."""
    rs = [r for r in rows if r.get("section") == "printed" and r.get("quantity") == "area"
          and isinstance(r.get("value"), (int, float)) and not r.get("review")]
    parts = [r for r in rs if not _TOTAL_RX.search(str(r.get("item") or ""))]
    groups: dict = {}
    for r in parts:
        name = _clean(re.sub(r"\(.*?\)", "", str(r.get("item") or "")))
        t = next((m.group(0) for w in _SPLIT_TYPES if (m := re.search(rf"\b{w}\b", name, re.I))), None)
        t = (t or _ZONE_RX.sub("", name) or name).strip().lower()
        g = groups.setdefault((t, r.get("unit")), {"type": t, "unit": r.get("unit"), "value": 0.0, "rows": [], "items": []})
        g["value"] += float(r["value"])
        g["rows"].append(r.get("id"))
        g["items"].append(r.get("item"))
    if not any(len(g["rows"]) > 1 for g in groups.values()):
        return None
    types = sorted(groups.values(), key=lambda g: -g["value"])
    for g in types:
        g["value"] = round(g["value"], 2)
    total = sum(g["value"] for g in types)
    tots = [r for r in rs if _TOTAL_RX.search(str(r.get("item") or ""))]
    check = None
    if tots:  # the printed total these types add up to (a SUBTOTAL before a half-counted stair, or the TOTAL itself)
        best = min(tots, key=lambda r: abs(float(r["value"]) - total))
        d = (total - float(best["value"])) / float(best["value"]) if best["value"] else None
        check = {"printed": best.get("item"), "printed_value": best["value"], "sum": round(total, 2),
                 "diff_pct": round(100 * d, 2) if d is not None else None, "agree": d is not None and abs(d) <= 0.005,
                 "id": best.get("id")}
    return {"types": types, "sum": round(total, 2), "check": check}


MODEL_DIFF_PCT = 5.0  # a room whose wall-line trace differs from its IfcSpace by more: listed for a person to look at


def _model_check(link: dict | None, rows: list[dict]) -> dict | None:
    """The IFC link's counts + the rooms where the drawing and the model disagree by > MODEL_DIFF_PCT (or the trace gave
    nothing: Maleva VALVERUUM 0.0 vs 10.32): F6's check list / the card's 'model vs drawing' line. The row's value is
    the model's; 'traced' is ours."""
    if not link:
        return None
    off = []
    for r in rows:
        c = r.get("check") if r.get("quantity") == "area" and r.get("section") == "room" else None
        if not c or "diff_pct" not in c:
            continue
        d = c.get("diff_pct")
        if d is None or abs(d) > MODEL_DIFF_PCT:
            off.append({"id": r.get("id"), "item": r.get("item"), "model": r.get("value"), "traced": c.get("value"),
                        "diff_pct": d, "focus": (r.get("evidence") or [{}])[0].get("bbox") or r.get("label_box")})
    off.sort(key=lambda o: -(abs(o["diff_pct"]) if o["diff_pct"] is not None else 1e9))
    return {**link, "differ_over_pct": MODEL_DIFF_PCT, "differ": off}


def _scale_text(st: dict) -> dict:
    n = st.get("scale_n")
    out = {"n": n, "text": f"1:{n:g}" if isinstance(n, (int, float)) and n else None}
    if str(st.get("kind") or "").startswith("ifc"):
        out.update(text="model units", source="measured in the building model (IFC): no drawing scale needed")
    elif (st.get("scale_calib") or {}).get("n"):  # no printed 1:N: the scale that fits the printed room areas
        c = st["scale_calib"]
        out.update(text=f"≈ 1:{c['n']:g}" + ("" if c.get("agree", 0) >= 2 else " (check)"),
                   source=f"derived {c['how']}" + (f"; picture traced into lines: {st['traced']}" if st.get("traced")
                                                   else ""))
    elif st.get("traced"):
        out["source"] = f"picture traced into lines: {st['traced']}"
    elif st.get("untraced"):
        out["source"] = f"picture not traced: {st['untraced']}"
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
                    # coverage (UX 02:05: '3 rooms · 22 m' on a 25-room floor read as the floor's skirting): rooms =
                    # rooms with a skirting READING (glance: rooms - review = in the total), of `labelled` rooms
                    "rooms": sum(1 for r in base_all if isinstance(r.get("value"), (int, float))),
                    "review": sum(1 for r in base_all if r.get("review") and isinstance(r.get("value"), (int, float))),
                    "labelled": len({(r.get("item"), r.get("copy", 1)) for r in rows
                                     if r.get("section") == "room" and r.get("quantity") == "area"}),
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
            "scale": _scale_text(st), "unit": unit, "rooms_cut": st.get("rooms_cut"),
            "by_storey": [{k: v for k, v in x.items() if not k.endswith("_guids")} for x in st.get("storeys") or []],
            "area": area, "rooms": {"labelled": len(area_rows),
                                                          "measured": sum(t["measured"] for t in types),
                                                          "review": sum(t["review"] for t in types)},
            "by_type": types, "doors": doors, "windows": windows, "skirting": skirting,
            "slabs": [{"item": r.get("item"), **num(r)} for r in rows if r.get("section") == "slab"],
            "printed": [{"item": r.get("item"), **num(r)} for r in rows if r.get("section") == "printed"],
            "printed_by_type": printed_by_type(rows),
            "by_name": by_name(rows),
            "review": review, "model_check": _model_check(st.get("ifc"), rows), "seconds": st.get("seconds"),
            "finished": st.get("finished"),
            "cost": st.get("cost") or {"tier": "cheap", "api_calls": 0}, "engine": st.get("engine")}


IMAGE_EXT = (".png", ".jpg", ".jpeg")
# the detector's whole-sheet pass on X1's DfE scan took 123 s for '1 window' (clean PDF: 79): cap it on pictures
PICTURE_SHEET_BUDGET_S = 90.0
SHEET_ON_PICTURE = os.environ.get("STOA_TAKEOFF_SHEET_PICTURE", "1") != "0"
TRACED_CAP = 0.5  # = stoa.solvers.upload.VECTOR_CAP: lines traced from a picture
UNTRACED = ("its paper size is unknown (no DPI tag, no standard export size it names, no photographed sheet corners, no "
            "metric scale bar with a printed 1:N), so rooms are not measured; calibrate the scale with Measure (two points "
            "+ the real length)")


def traced(img: str) -> tuple[str, float, str] | None:
    """A picture of a CAD sheet -> P1's traced vector copy (stoa.formats.vectorize: the image's lines as PDF strokes,
    its OCR text as invisible text, cached next to the upload) + the factor pdf_pt -> image px + how the paper size
    was found. The same step the upload solver takes when its image readers find nothing (Upload._as_vector)."""
    from stoa.formats import vectorize as V

    hit, how = paper_source(img)
    if hit is None:
        return None
    dpi, sheet = hit
    return str(V.traced_copy(img, dpi)), dpi / 72.0, f"{sheet} at {dpi:.0f} dpi {how}"


FI_ASSUMED_LONG_IN = 297 / 25.4  # an apartment plan of no known paper size is traced as if it filled an A4 sheet


def fi_trace(img: str) -> tuple[str, float, str] | None:
    """A picture with no paper size the readers find, whose printed words are a Finnish apartment plan's room codes
    WITH their m² ('OH' over '16,3'): traced at the dpi of an A4 sheet. The dpi only fixes the traced copy's units:
    the scale then comes from the printed room areas (calibrate_printed), never from the assumed paper."""
    try:
        from stoa.formats import vectorize as V
        from stoa.solvers.raster import textlayer as TL

        texts = [i.text for i in TL.text_layer(img).items]
        if not fi_sheet(texts) or not any(_FI_NUM.match(" ".join(str(t).split())) for t in texts):
            return None
        from PIL import Image

        with Image.open(img) as im:
            dpi = max(im.size) / FI_ASSUMED_LONG_IN
        return (str(V.traced_copy(img, dpi)), dpi / 72.0,
                f"traced at {dpi:.0f} dpi (paper size unknown); the scale from the room areas printed on the plan")
    except Exception:  # noqa: BLE001
        return None


def deskewed(img: str):
    """O1's deskewed copy of a phone photo of a printed sheet (stoa.solvers.raster.photo.cached_deskew, cached in the
    same place the upload's prepare() puts it, so /ask and the takeoff read ONE copy), or None for any other picture.
    W6's DfE photo traced raw (one scale bar for a tilted sheet): classrooms -11 %, kitchen +10 % vs X1 golds."""
    try:
        from stoa.formats.normalize import _cache_dir
        from stoa.solvers.raster.photo import cached_deskew

        return cached_deskew(img, _cache_dir(Path(img)))
    except Exception:  # noqa: BLE001 — not a photo we can rectify: trace the picture as it is
        return None


def paper_source(img: str) -> tuple[tuple[float, str] | None, str]:
    """((dpi, sheet), how) for a picture, in the upload solver's order (Upload._as_vector, so both trace the SAME copy at
    the same dpi): DPI metadata, the export's pixel size + the sheet size it prints, a deskewed phone photo's corners +
    the sheet size it prints (O1 20:48), the scale bar x the printed 1:N (X1 19:20: scans and crops of no standard
    size). Before 22:20 the takeoff had only the first two, so a phone photo or an odd-size scan gave no rooms at all."""
    from stoa.formats import vectorize as V

    hit = V.paper(img)
    if hit is not None:
        return hit, "from its DPI metadata"
    try:
        from stoa.formats.paper import paper_of_image
        from stoa.solvers.raster import textlayer as TL

        layer = TL.text_layer(img)
        texts = [i.text for i in layer.items]
    except Exception:  # noqa: BLE001 — no text layer: no paper source below can work
        return None, ""
    for how, find in (("from its pixel size and the sheet size it prints", lambda: paper_of_image(img, texts)),
                      ("", lambda: paper_by_dims(img, layer)),
                      ("from the photographed sheet's corners and the sheet size it prints", lambda: _photo_paper(img, texts))):
        try:
            hit = find()
        except Exception:  # noqa: BLE001
            hit = None
        if hit is not None:
            return (hit[:2], hit[2]) if len(hit) > 2 else (hit, how)
    try:
        from stoa.formats.paper import paper_of_bar

        bar = paper_of_bar(img, layer)
    except Exception:  # noqa: BLE001
        bar = None
    if bar is not None:
        return bar[:2], bar[2]
    return None, ""


DIM_TOL = 0.02  # a dimension string's drawn line within 2 % of the length its paper + printed scale give it


def paper_by_dims(img: str, layer) -> tuple[float, str, str] | None:
    """(dpi of THIS file, sheet, how) when the export's pixel size fits SEVERAL standard sheets (paper.py: 10200 x 6600
    = ANSI D @ 300 = Tabloid @ 600) and the sheet names none: the sheet whose printed 1:N makes the drawing's own
    imperial dimension strings ('286'-0"') as long as their drawn lines (raster.dimline) wins, when >= 2 of them agree
    within DIM_TOL and no other sheet has as many. AFCEC A-2 (6000-px copy): 286'-0", 323'-4", 80', 52'-8" give
    8.84-8.98 px/ft = Tabloid @ 600 at 1" = 40' (8.82), none near ANSI D @ 300 (4.41) — the PDF twin's page is Tabloid."""
    from PIL import Image

    from stoa.formats import paper as P
    from stoa.formats.vectorize import _scale_n
    from stoa.solvers.raster import dimline as DL
    from stoa.solvers.raster.textlayer import dimensions

    Image.MAX_IMAGE_PIXELS = None
    n = _scale_n(layer.items)
    dims = [d for d in dimensions(layer) if d["feet"] >= 4][:60]
    if not n or len(dims) < 2:
        return None
    with Image.open(img) as im:
        size = im.size
        raw = str(im.info.get(P.SRC_SIZE_KEY) or "")
        m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", raw)
        src = (int(m.group(1)), int(m.group(2))) if m else P._sidecar_src_size(img, size) or size
        cands = {}
        for name, _ in P.candidates(src):
            sw, sl = P.PAPER_MM[name]
            a, b = sorted(src)
            cands[name] = (a / (sw / 25.4) + b / (sl / 25.4)) / 2 * size[0] / src[0]  # dpi of this file
        if len(cands) < 2:
            return None
        dark = DL._dark(im)  # once: dim_line thresholds a PIL image again on every call
        rates = []
        for d in dims:
            ln = DL.dim_line(dark, d["box"])
            if ln and ln.get("px"):
                rates.append(float(ln["px"]) / d["feet"])
    support = {c: sum(1 for r in rates if abs(r / (dpi * 12.0 / n) - 1) <= DIM_TOL) for c, dpi in cands.items()}
    best = max(support, key=support.get)
    if support[best] < 2 or sum(1 for v in support.values() if v == support[best]) > 1:
        return None
    dpi = cands[best]
    return dpi, best, (f"from its pixel size ({' or '.join(sorted(cands))} at a usual export dpi) and its dimension "
                       f"strings: {support[best]} of {len(rates)} drawn lines match 1:{n:g} on {best}")


def _photo_paper(img: str, texts: list[str]):
    from stoa.solvers.raster.photo import named_paper

    return named_paper(img, texts)


def px_to_pt(rows: list[dict], virtual: str, zoom: float) -> None:
    """Evidence in a scanned PDF's render pixels (image_px, zoom px per pt) -> that PDF's page 0 points."""
    def box(b):
        return [round(v / zoom, 2) for v in b]

    for r in rows:
        for e in r.get("evidence") or []:
            if e.get("bbox_space") != "image_px":
                continue
            e["file"], e["page"], e["bbox_space"] = virtual, 0, "pdf_pt"
            if e.get("bbox"):
                e["bbox"] = box(e["bbox"])
            for k in ("polygon", "polyline"):
                if e.get(k):
                    e[k] = [[round(x / zoom, 2), round(y / zoom, 2)] for x, y in e[k]]
        if r.get("label_box"):
            r["label_box"] = box(r["label_box"])


def to_image(rows: list[dict], virtual: str, f: float, note: str, H=None) -> None:
    """Evidence read on the traced copy -> the uploaded picture's pixels; confidence capped like the upload solver.
    H (3x3, deskewed px -> photo px): a phone photo was traced as O1's deskewed copy; its boxes go back onto the photo."""
    if H is not None:
        from stoa.solvers.raster.photo import map_box, map_points

        box = lambda b: [round(v, 1) for v in map_box(H, [v * f for v in b])]  # noqa: E731
        pts = lambda ps: [[round(x, 1), round(y, 1)] for x, y in map_points(H, [[x * f, y * f] for x, y in ps])]  # noqa: E731
    else:
        box = lambda b: [round(v * f, 1) for v in b]  # noqa: E731
        pts = lambda ps: [[round(x * f, 1), round(y * f, 1)] for x, y in ps]  # noqa: E731
    for r in rows:
        if r.pop("_px", False):
            continue
        for e in r.get("evidence") or []:
            e["file"], e["page"], e["bbox_space"] = virtual, None, "image_px"
            if e.get("bbox"):
                e["bbox"] = box(e["bbox"])
            for k in ("polygon", "polyline"):
                if e.get(k):
                    e[k] = pts(e[k])
        if r.get("label_box"):
            r["label_box"] = box(r["label_box"])
        if isinstance(r.get("confidence"), (int, float)) and r["confidence"] > TRACED_CAP:
            r["confidence"] = TRACED_CAP
        if r.get("value") is not None and r.get("section") != "total":
            r["assumptions"] = [*(r.get("assumptions") or []), f"read on the picture's lines traced into a vector "
                                                              f"drawing ({note}); confidence capped at {TRACED_CAP}"]
            if "scale bar" in note and r.get("quantity") in ("area", "baseboard"):
                # one scale for the whole picture, from its bar: on a phone photo / skewed scan the scale drifts across
                # the sheet (W6's DfE photo: classrooms -11 %, kitchen +10 %, fitness room -2 % vs X1 golds)
                r["assumptions"].append("photo / scan scaled from its one scale bar, perspective not corrected: "
                                        "expect up to ±10 % away from the bar")


IFC_CONF = {"area": 0.95, "baseboard": 0.8, "doors": 0.85, "windows": 0.85}
DOOR_W_MAX = 3.0  # m: a door's width from its model box (wider = a box skewed by a diagonal wall: not deducted)


def ifc_rows(abs_path: str, virtual: str, log=print) -> tuple[list[dict], dict]:
    """The takeoff of a BUILDING MODEL (an uploaded / dataset .ifc): every IfcSpace = a room (its storey in the name),
    area = the model's net floor area (Qto) else its footprint, skirting = the space outline's perimeter minus the
    widths of the doors that touch it (door width from its model box), doors / windows per room by geometric adjacency
    (the IFC solver's rule), whole-model door / window totals. Evidence = the element's GlobalId (the 3D view highlights
    it). Placeholder spaces (about 1 m² each) give no rooms, only the totals, and say so. No model calls."""
    from shapely.geometry import Polygon

    from stoa.solvers.ifc.factcard import placeholder_spaces
    from stoa.solvers.ifc.index import load_index
    from stoa.solvers.ifc.solver import adjacent

    idx = load_index(abs_path)
    storeys = {s.guid: s for s in idx.storeys}
    info: dict = {"spaces": 0, "project": idx.project_long_name or idx.project_name, "schema": idx.schema}

    def ev(e, note):
        return {"file": virtual, "page": None, "ifc_guid": e.guid, "note": note}

    def qto(e, *keys):
        for k, v in (e.qto or {}).items():
            if any(k.endswith("." + x) for x in keys) and isinstance(v, (int, float)) and v > 0:
                return float(v)
        return None

    def door_w(d) -> float | None:
        w = qto(d, "Width", "OverallWidth")
        if w is None and d.geo and d.geo.bbox:
            b = d.geo.bbox
            w = max(b[3] - b[0], b[4] - b[1])
        return w if w and 0.3 <= w <= DOOR_W_MAX else None

    rows: list[dict] = []
    spaces = idx.by_class("IfcSpace")
    if spaces and placeholder_spaces(idx):
        info["note"] = ("the model's IfcSpaces are placeholders (about 1 m² each): it holds no room areas, so only the "
                        "door and window totals are listed")
        spaces = []
    elev = {g: (s.elevation if s.elevation is not None else 0.0) for g, s in storeys.items()}

    def label(e) -> str:
        a, b = (e.long_name or "").strip(), (e.name or "").strip()
        return f"{a} {b}".strip() if a and b and a != b else (a or b or "Space")

    spaces = sorted(spaces, key=lambda e: (elev.get(e.storey, 0.0), label(e)))
    names: dict[str, int] = {}
    for e in spaces:
        st_name = (storeys.get(e.storey).long_name or storeys.get(e.storey).name) if storeys.get(e.storey) else None
        base = label(e)
        item = f"{base} ({st_name})" if st_name else base
        names[item] = names.get(item, 0) + 1
        common = {"section": "room", "item": item, "copy": names[item], "copies": 1, "storey": st_name,
                  "status": "ok", "ifc_guid": e.guid}
        area = qto(e, "NetFloorArea", "GrossFloorArea") or (e.geo.footprint_area if e.geo else None)
        if not area or area <= 0.2:
            continue
        info["spaces"] += 1
        how = "the model's quantity set" if qto(e, "NetFloorArea", "GrossFloorArea") else "the space's footprint"
        rows.append({**common, "quantity": "area", "unit": "m2", "value": round(area, 2),
                     "confidence": IFC_CONF["area"], "method": f"IFC model: IfcSpace area ({how})",
                     "evidence": [ev(e, f"IfcSpace '{base}' on {st_name or 'its storey'}: {area:.2f} m² ({how})")]})
        doors = adjacent(idx, e, ("IfcDoor",))
        wins = adjacent(idx, e, ("IfcWindow",))
        ring = None
        if e.geo and e.geo.footprint:
            try:
                ring = max((Polygon(r) for r in e.geo.footprint if len(r) >= 3), key=lambda g: g.area)
            except ValueError:
                ring = None
        per = qto(e, "NetPerimeter", "GrossPerimeter") or (ring.length if ring is not None else None)
        if per and not _no_skirting(base):
            ws = [w for w in map(door_w, doors) if w]
            val = per - sum(ws)
            rows.append({**common, "quantity": "baseboard", "unit": "m", "value": round(val, 2),
                         "confidence": IFC_CONF["baseboard"],
                         "method": f"IFC model: space perimeter {per:.2f} m minus {len(ws)} door width"
                                   f"{'s' if len(ws) != 1 else ''} {sum(ws):.2f} m",
                         "assumptions": [f"perimeter {per:.2f} m minus {len(ws)} opening(s)",
                                         "door widths from the doors' model boxes"],
                         "parts": {"perimeter_m": round(per, 3), "openings_m": [round(w, 3) for w in ws]},
                         "evidence": [ev(e, f"IfcSpace '{base}': perimeter {per:.2f} m")]
                         + [ev(d, f"door {d.name or ''} ({d.type_name or 'door'}): opening {w:.2f} m".strip())
                            for d in doors if (w := door_w(d))]})
        rows.append({**common, "quantity": "doors", "unit": "count", "value": len(doors),
                     "confidence": IFC_CONF["doors"], "method": "IFC model: IfcDoor elements touching the space",
                     "evidence": [ev(d, f"door {d.name or ''} ({d.type_name or 'door'})") for d in doors]})
        rows.append({**common, "quantity": "windows", "unit": "count", "value": len(wins),
                     "confidence": IFC_CONF["windows"], "method": "IFC model: IfcWindow elements touching the space",
                     "evidence": [ev(w, f"window {w.name or ''} ({w.type_name or 'window'})") for w in wins]})
    # slabs by type (the estimator's 'slab areas by type'): the model's own areas, one evidence per slab
    groups: dict = {}
    for e in idx.by_class("IfcSlab"):
        a = qto(e, "NetArea", "GrossArea") or (e.geo.footprint_area if e.geo else None)
        if a and a > 0.05:
            k = (e.type_name or e.object_type or e.name or "Slab").strip()
            groups.setdefault((k, e.predefined_type or ""), []).append((e, a))
    for (k, pt), els in sorted(groups.items(), key=lambda kv: -sum(a for _, a in kv[1])):
        tot = sum(a for _, a in els)
        kind = {"FLOOR": "floor", "ROOF": "roof", "BASESLAB": "base slab", "LANDING": "landing"}.get(pt.upper(), pt.lower())
        rows.append({"section": "slab", "item": k, "quantity": "slab_area", "unit": "m2", "value": round(tot, 2),
                     "confidence": 0.9, "status": "ok", "copy": 1, "copies": 1,
                     "method": f"IFC model: {len(els)} IfcSlab{'s' if len(els) != 1 else ''} of this type"
                               + (f" ({kind})" if kind else "") + ", plan area summed",
                     "evidence": [ev(e, f"slab {e.name or ''}: {a:.2f} m²") for e, a in els][:MAX_MARKS]})
    # walls by type: length along the wall's axis, one face's area = length x height (gross: openings not deducted)
    walls: dict = {}
    for e in idx.by_class("IfcWall", "IfcWallStandardCase"):
        ln = qto(e, "Length") or e.axis_length
        if not ln or ln <= 0.05:
            continue
        b = e.geo.bbox if e.geo else None
        h = qto(e, "Height") or ((b[5] - b[2]) if b else None)
        side = qto(e, "NetSideArea", "GrossSideArea") or (ln * h if h and 0.2 < h < 30 else None)
        walls.setdefault((e.type_name or e.object_type or e.name or "Wall").strip(), []).append((e, ln, side))
    for k, els in sorted(walls.items(), key=lambda kv: -sum(x[1] for x in kv[1])):
        ext = [e for e, _, _ in els if e.props.get("Pset_WallCommon.IsExternal") is True]
        where = " (external)" if len(ext) == len(els) else " (internal)" if not ext else ""
        evs = [ev(e, f"wall {e.name or ''}: {ln:.2f} m") for e, ln, _ in els][:MAX_MARKS]
        base = {"section": "wall", "item": k + where, "status": "ok", "copy": 1, "copies": 1, "evidence": evs}
        rows.append({**base, "quantity": "wall_length", "unit": "m", "value": round(sum(x[1] for x in els), 2),
                     "confidence": 0.9, "method": f"IFC model: {len(els)} IfcWall of this type, axis lengths summed"})
        sides = [x[2] for x in els if x[2]]
        if sides:
            rows.append({**base, "quantity": "wall_area", "unit": "m2", "value": round(sum(sides), 2),
                         "confidence": 0.8, "method": f"IFC model: {len(sides)} walls, length x height summed (one "
                                                     f"face, openings not deducted)"})
    if not info["spaces"] and not info.get("note"):
        info["note"] = ("the model has no rooms (no IfcSpace): its doors, windows and slabs are listed; rooms can be "
                        "measured on a plan sheet of it")
    # per storey (an estimator prices floor by floor): rooms + net area from the rows above, doors / windows by the
    # model's storey containment (Esplanades: 46 / 42 / 42 / 42 / 39 doors = the dataset's per-floor golds)
    per: dict = {}
    for r in rows:
        if r.get("quantity") == "area":
            d = per.setdefault(r.get("storey"), {"rooms": 0, "area": 0.0, "ids": []})
            d["rooms"] += 1
            d["area"] += float(r["value"])
    by_st = []
    for g, s_ in sorted(storeys.items(), key=lambda kv: elev.get(kv[0], 0.0)):
        name = s_.long_name or s_.name
        dn, wn = idx.in_storey(g, "IfcDoor"), idx.in_storey(g, "IfcWindow")
        d = per.get(name) or {"rooms": 0, "area": 0.0}
        if d["rooms"] or dn or wn:
            by_st.append({"storey": name, "elevation_m": s_.elevation, "rooms": d["rooms"],
                          "area": round(d["area"], 2), "unit": "m2", "doors": len(dn), "windows": len(wn),
                          "door_guids": [e.guid for e in dn][:MAX_MARKS], "window_guids": [e.guid for e in wn][:MAX_MARKS]})
    info["storeys"] = by_st
    for q, cls in (("doors", "IfcDoor"), ("windows", "IfcWindow")):
        els = idx.by_class(cls)
        rows.append({"section": "sheet", "item": "Whole model", "quantity": q, "unit": "count", "value": len(els),
                     "confidence": 0.95, "status": "ok", "method": f"IFC model: every {cls}",
                     "evidence": [ev(d, f"{cls[3:].lower()} {d.name or ''} ({d.type_name or ''})")
                                  for d in els][:MAX_MARKS]})
    log(f"[takeoff] IFC model: {info['spaces']} spaces, {len(rows)} rows")
    return rows, info


def run(abs_path: str, virtual: str, page: int = 0, out_dir: str | Path | None = None, log=print) -> dict:
    """The whole takeoff of one sheet; progress.json / result.json in out_dir (when given) after every row.
    A PNG / JPEG is read through its traced vector copy (see traced()), evidence mapped back to its pixels.
    An .ifc building model is read from its IfcSpaces / doors / windows (ifc_rows)."""
    t0 = time.time()
    out_dir = Path(out_dir) if out_dir else None
    if abs_path.lower().endswith(".ifc"):
        if out_dir:
            _write_json(out_dir / "progress.json", {"status": "running", "file": virtual, "page": 0, "done": 0,
                                                    "total": 1, "current": "reading the model's rooms, doors, windows"})
        rows, info = ifc_rows(abs_path, virtual, log)
        state = {"status": "done", "file": virtual, "page": 0, "engine": ENGINE_V, "kind": "ifc model",
                 "started": t0, "done": 1, "total": 1, "current": "", "rooms": info["spaces"], "slabs": [],
                 "source": "IfcSpace rooms of the building model", "scale_n": None, "rows": rows,
                 "sheet": info.get("project"), "revision": None, "ifc_note": info.get("note"),
                 "storeys": info.get("storeys") or [],
                 "finished": time.time(), "seconds": round(time.time() - t0, 1),
                 "cost": {"api_calls": 0, "llm_tokens_in": 0, "llm_calls_replayed": 0, "tier": "cheap"}}
        if out_dir:
            _write_json(out_dir / "result.json", state)
            _write_json(out_dir / "progress.json", {k: v for k, v in state.items() if k != "rows"})
        return state
    image, f, how, photo_h = None, 1.0, "", None
    scan_zoom = None  # a SCANNED PDF (page 0 = one picture, no text): read like a picture, evidence back in pdf_pt
    if abs_path.lower().endswith(".pdf") and not page:
        try:
            from stoa.formats.normalize import scan_png

            sp = scan_png(abs_path)
        except Exception:  # noqa: BLE001
            sp = None
        if sp:
            pdf_abs, (png, scan_zoom) = abs_path, sp
            if out_dir:
                _write_json(out_dir / "progress.json", {"status": "running", "file": virtual, "page": 0, "done": 0,
                                                        "total": 0, "current": "a scanned PDF: tracing its picture's lines"})
            photo = deskewed(png)
            tr = traced(photo.path if photo else png)
            if tr is not None:
                image, (abs_path, f, how) = pdf_abs, tr
                if photo:
                    photo_h = photo.to_photo
                    how = f"{how}; its perspective undone from the sheet's corners first ({photo.paper} sheet)"
                how = f"a scanned PDF read as its picture: {how}"
                log(f"[takeoff] scanned PDF traced: {abs_path} ({how})")
            else:
                scan_zoom = None
    if abs_path.lower().endswith(IMAGE_EXT):
        if out_dir:
            _write_json(out_dir / "progress.json", {"status": "running", "file": virtual, "page": page, "done": 0,
                                                    "total": 0, "current": "tracing the picture's lines (once per file)"})
        photo = deskewed(abs_path)  # a phone photo of a print: O1's rectified copy (the one /ask reads), traced instead
        tr = traced(photo.path if photo else abs_path)
        if tr is None and photo is not None and (getattr(photo, "dpi", None) or 0) > 30:
            # the rectified copy names no paper size the readers above find, but O1 sized it from the photographed
            # sheet's corners (ISO A ratio; A3 when the sheet names no size) = the pixel scale /ask uses (F6 00:10:
            # X1's Pagen phone photo gave 0 rooms)
            from stoa.formats import vectorize as V

            tr = (str(V.traced_copy(photo.path, float(photo.dpi))), float(photo.dpi) / 72.0,
                  f"{photo.paper} sheet at {float(photo.dpi):.0f} dpi from the photographed sheet's corners (A3 assumed "
                  "when the sheet names no size)")
        if tr is None and photo is None:
            tr = fi_trace(abs_path)  # a Finnish apartment plan with no paper size: its printed room m² give the scale
        if tr is not None:
            image, (abs_path, f, how) = abs_path, tr
            if photo:
                photo_h = photo.to_photo
                how = f"{how}; a phone photo: its perspective undone from the sheet's corners first ({photo.paper} sheet)"
            page = 0
            log(f"[takeoff] picture traced: {abs_path} ({how})")
    raw_img = image is None and abs_path.lower().endswith(IMAGE_EXT)  # a picture we could not trace (no paper size)
    plan = plan_items(abs_path, page, picture=image is not None,
                      picture_src=(png if scan_zoom else image) or (abs_path if raw_img else None))
    items = plan["items"]
    n = _scale(abs_path, page) if plan["kind"] == "vector-pdf" else None
    calib = None
    if n is None and plan["kind"] == "vector-pdf" and any(r.get("printed") for r in plan["rooms"]):
        calib = calibrate_printed(abs_path, page, plan["rooms"])
        n = (calib or {}).get("n")
        nts = _not_to_scale(abs_path, page)
        if calib and nts:  # 'SUUNTAA-ANTAVA, EI MITTAKAAVASSA' = indicative, not to scale: say it with every number
            calib["how"] = f"{calib['how']}; the plan itself says '{nts}' (not to scale)"
        log(f"[takeoff] no printed scale; from the printed room areas: {calib}")
    rows: list[dict] = []
    state = {"status": "running", "file": virtual, "page": page, "engine": ENGINE_V, "kind": plan["kind"],
             "started": t0, "done": 0, "total": len(items), "current": "", "rooms": len(plan["rooms"]),
             "slabs": [s["text"] for s in plan["slabs"]], "source": plan.get("source"), "scale_n": n,
             "rows": rows}
    if calib:
        state["scale_calib"] = calib
    state["sheet"], state["revision"] = sheet_rev(abs_path, page)
    if raw_img:  # say why, so the panel / glance card can point at Measure's calibration instead of showing nothing
        state["untraced"] = UNTRACED
    if plan.get("cut"):
        state["rooms_cut"] = {"measured": len(plan["rooms"]), "not_measured": len(plan["cut"]),
                              "names": plan["cut"][:40], "limit": MAX_ROOMS}
    for i, it in enumerate(items):
        state["current"] = f"{it['item']}: {QUANTITY_LABEL.get(it.get('quantity', 'area'), '')}".strip(": ")
        if out_dir:
            _write_json(out_dir / "progress.json", state)
        if it.get("kind") == "trace":
            rows.extend(measure_copy(abs_path, virtual, page, it["room"], n))
        elif it.get("kind") == "tag":
            rows.append(tag_rows(virtual, page, it["tag"]))
        elif it.get("kind") == "tagtrace":
            tag_trace(abs_path, virtual, page, it["tag"], n, rows)
        elif it.get("kind") == "equipment":  # a piping plan's tag families (piping_rows), file = the upload's name
            r = dict(it["row"])
            r["evidence"] = [dict(e, file=virtual) for e in r["evidence"]]
            rows.append(r)
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
            # a picture's whole-sheet doors / windows: asked on the PICTURE, the reader a visitor's own question uses
            # (one number on the card and in the takeoff), not on its traced copy, whose traced 'arcs' gave AFCEC A-2
            # 494 doors (the picture: 153) and DfE L00 PNG 39 windows (picture 76; PDF 69)
            on_pic = bool(image and it["section"] == "sheet" and not scan_zoom and SHEET_ON_PICTURE
                          and str(image).lower().endswith(IMAGE_EXT))
            out = ask(image if on_pic else abs_path, virtual, it["question"], 0 if on_pic else page, budget_s=budget,
                      solver=solver, detector=det)
            rows.append(row_from_answer(base, out))
            if on_pic:
                rows[-1]["_px"] = True  # its marks are in the picture's pixels already: to_image leaves them
                for e in rows[-1].get("evidence") or []:
                    e["file"], e["bbox_space"] = virtual, e.get("bbox_space") or "image_px"
                if rows[-1].get("value") is not None:
                    rows[-1]["assumptions"] = [*(rows[-1].get("assumptions") or []),
                                               "counted on the picture itself (the reader your own question uses)"]
            lb = it.get("label_box")
            if it["section"] == "room" and q == "area" and n and lb and len(lb) == 4 and \
                    not (isinstance(rows[-1].get("value"), (int, float)) and rows[-1]["value"] > 0):
                # asked by name, a reader gave no area or 0 m² (Esplanades floor 1 'VALVERUUM' = 0.0 from the model
                # link): trace the room at its own label instead, so it is not missing from the table
                got = [g for g in measure_copy(abs_path, virtual, page, {
                    "name": it["item"], "copy": it.get("copy", 1), "copies": it.get("copies", 1), "bbox": lb,
                    "pt": [(lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2]}, n) if g["quantity"] == "area"]
                if got and isinstance(got[0].get("value"), (int, float)) and got[0]["value"] > 0:
                    got[0]["assumptions"] = [*(got[0].get("assumptions") or []),
                                             f"asked by name, the readers gave {rows[-1].get('value')!r}: traced instead"]
                    rows[-1] = got[0]
            log(f"[takeoff] {i + 1}/{len(items)} {it['question']} -> {out.get('value')!r} {out.get('unit') or ''} "
                f"({out.get('status')}, {out.get('seconds')} s)")
        state["done"] = i + 1
    if not image and plan["rooms"]:
        link = ifc_spaces(abs_path, virtual, rows, page)
        if link:
            state["ifc"] = link
            log(f"[takeoff] IfcSpace outlines on the sheet: {link}")
    if plan["rooms"]:
        k = doors_by_room(abs_path, virtual, rows, page, scale_n=n if calib else None)
        log(f"[takeoff] doors per room from the swing arcs on each traced outline: {k} rooms")
    if (not image or OFFWALL_PICTURES) and plan["rooms"] and n:
        for r in rows:  # outlines that do not follow drawn walls (leaked / caught furniture) -> Review at read time
            if r.get("section") == "room" and r.get("quantity") == "area" and isinstance(r.get("value"), (int, float)) \
                    and "ifc" not in str(r.get("method") or "").lower():
                poly = next((e.get("polygon") for e in r.get("evidence") or []
                             if e.get("polygon") and (e.get("bbox_space") or "pdf_pt") == "pdf_pt"), None)
                if poly:
                    r["off_wall"] = off_wall(abs_path, page, n, poly)
    if not image and plan["rooms"]:
        k = windows_by_room(abs_path, virtual, rows, page=page)
        log(f"[takeoff] windows per room from the linework (or the sheet's detector pass): {k} rooms")
    if any(r.get("fi") for r in plan["rooms"]):
        fi_rows(rows, plan["rooms"], virtual, page, calib)
    if image:
        to_image(rows, virtual, f, how, photo_h)
        state.update(kind="image (traced)", traced=how)
        if scan_zoom:  # the picture's pixels -> the scanned PDF's points (the viewer shows the PDF)
            px_to_pt(rows, virtual, scan_zoom)
            state.update(kind="scanned pdf (traced)")
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
            "evidence_box": (" ".join(f"{v:.0f}" for v in ev.get("bbox") or []) + (f" {ev.get('bbox_space')}"
                                                                                   if ev.get("bbox") else ""))
            # a building model's row: the element's GlobalId (findable in any BIM viewer)
            or (f"IFC {ev['ifc_guid']}" if ev.get("ifc_guid") else ""),
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
        if r.get("section") in ("printed", "slab", "sheet", "total", "same_name", "printed_type"):
            ft = " [ft²]" if r.get("unit") == "ft2" else ""
            what = {"printed": "printed", "same_name": "sum of the rooms with this name",
                    "printed_type": "printed, summed by type"}.get(r.get("section"),
                                                                    QUANTITY_LABEL.get(r.get("quantity", ""), r.get("quantity", "")))
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
    for r in rows:  # a piping plan's tagged equipment: the count in the item text (no count column on a room table)
        if r.get("section") == "equipment":
            out.append([sh(r), pg(r), f"{r.get('item')}: {r.get('value')} tagged", "", "", "", "", "", "", "", "", "",
                        r.get("review") or "", f"{r.get('method', '')}; {(r.get('assumptions') or [''])[0]}"])
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


def status(key: str, virtual: str | None = None, visitor: str | None = None) -> dict:
    """The job's state; a finished one through finish() (ids, sheet, room types, review, totals) with THIS visitor's
    own fixes only (_fixes_path)."""
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
                st = finish(st, virtual, fixes=_fixes(d, visitor))
            if name == "progress.json" and st.get("status") == "running":
                pid = _pid(d)
                if pid and not _alive(pid):
                    st["status"] = "failed"
                    st["error"] = "the takeoff process stopped: " + _tail(d / "log.txt")
            return st
    return {"job": key, "status": "unknown"}


_VISITOR_RX = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _fixes_path(d: Path, visitor: str | None = None) -> Path:
    """Where a person's fixes live: one file PER VISITOR in the content-keyed job folder (the browser's 'stoa.visitor'
    id, the one F7 / T3 / F6 key their memories by), so one judge's Measure on a catalogue sheet never shows as
    'measured by a person' to the next visitor or on another server sharing the cache (F6 23:36, QA's F7 leak 21:43).
    The id is hashed into the name. No id (scripts, tests, the CLI, readers that pass none) -> 'fixes-anon.json';
    the old shared 'fixes.json' (rehearsals before 23:59) is never applied again."""
    v = str(visitor or "")
    if not v:
        return d / "fixes-anon.json"
    if not _VISITOR_RX.fullmatch(v):
        raise ValueError("bad visitor id")
    return d / f"fixes-{hashlib.sha256(v.encode()).hexdigest()[:16]}.json"


def _fixes(d: Path, visitor: str | None = None) -> dict:
    try:
        return json.loads(_fixes_path(d, visitor).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _job_file(d: Path) -> str:
    """The virtual path a job folder was made for (queued.json / result.json 'file'), '' when unreadable."""
    for name in ("queued.json", "result.json", "progress.json"):
        try:
            f = json.loads((d / name).read_text()).get("file")
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if f:
            return str(f)
    return ""


def forget(visitor: str, keep: list[str] | None = None) -> int:
    """'Start over' (captain 01:18, W5's window 'stoa:reset'): THIS visitor's Measure fixes are dropped from every
    takeoff, so the sample sheets read as they first did; the drawings in `keep` (the visitor's own uploads when they
    said 'keep my corrections' in W5's confirm, the same rule as F7's POST /feat/corrections/forget) keep theirs.
    Other visitors' fixes and the content-keyed jobs themselves are never touched. Returns how many files went."""
    if not visitor:
        return 0
    name = _fixes_path(Path("."), visitor).name  # ValueError on a bad id
    kept = {str(k) for k in (keep or []) if k}
    n = 0
    for fp in cache_dir().glob(f"*/{name}"):
        if kept and _job_file(fp.parent) in kept:
            continue
        try:
            fp.unlink()
            n += 1
        except OSError:
            pass
    return n


def fix(key: str, row_id: str, value, unit: str | None = None, polygon=None, space: str | None = None,
        item: str | None = None, visitor: str | None = None) -> dict:
    """A person's measurement for one row of a finished takeoff (value None = undo): kept in the job's fixes file of
    THIS visitor (_fixes_path), applied by finish() so their totals, exports and summary card count it (nobody else's).
    Returns the job's status as that visitor sees it."""
    d = job_dir(key)
    if not (d / "result.json").exists():
        raise ValueError("the takeoff is not finished")
    if not re.fullmatch(r"r\d{1,5}", row_id or ""):
        raise ValueError("bad row id")
    fp = _fixes_path(d, visitor)
    fx = _fixes(d, visitor)
    for k in [k for k, f in fx.items() if f.get("derived_from") == row_id]:
        fx.pop(k)  # the skirting derived from this row's earlier outline (re-measured or undone)
    if value is None:
        fx.pop(row_id, None)
    else:
        v = float(value)
        if not 0 < v < 1e7:
            raise ValueError("value out of range")
        poly = [[float(x), float(y)] for x, y in (polygon or [])][:2000] or None
        sp = space if space in ("pdf_pt", "image_px") else None
        fx[row_id] = {"value": v, "unit": (unit or "")[:8], "polygon": poly, "space": sp, "item": item,
                      "at": time.strftime("%H:%M")}
        if poly and len(poly) >= 3 and sp:
            try:
                sk = skirting_of_outline(key, row_id, v, poly, sp)
            except Exception:  # noqa: BLE001 - the area fix stands without it
                sk = None
            if sk:
                fx[sk.pop("row")] = {**sk, "derived_from": row_id, "space": sp, "item": item,
                                     "at": fx[row_id]["at"]}
    _write_json(fp, fx)
    return status(key, visitor=visitor)


def skirting_of_outline(key: str, area_id: str, area_m2: float, poly: list, space: str) -> dict | None:
    """A person traced a room's AREA with Measure: its skirting from THAT outline = the outline's perimeter minus the
    door openings found along it (F1's region.measure_shape, at the person's own scale: units per m from their polygon
    and their m²). Never mixed with the old outline's openings. None when the room has no skirting row, the room gets
    none (wet / no-skirting names keep theirs), or no opening reading came back."""
    st = status(key)
    rows = st.get("rows") or []
    a = next((r for r in rows if r.get("id") == area_id), None)
    if not a or a.get("quantity") != "area" or a.get("section") != "room":
        return None
    b = next((r for r in rows if r.get("section") == "room" and r.get("quantity") == "baseboard"
              and r.get("item") == a.get("item") and r.get("copy", 1) == a.get("copy", 1)), None)
    if not b or not b.get("id"):
        return None
    abs_path, _ = resolve(st.get("file") or "")
    from stoa.features.region import _shoelace, measure_shape

    area_u = abs(_shoelace([[float(x), float(y)] for x, y in poly]))
    if area_u <= 0 or area_m2 <= 0:
        return None
    upm = (area_u / area_m2) ** 0.5  # drawing units per metre, as the person's measurement implies
    m = measure_shape(abs_path, int(st.get("page") or 0), space, poly, "area", False, upm)
    sk, per = m.get("skirting_m"), m.get("perimeter_m")
    if not isinstance(sk, (int, float)) or not isinstance(per, (int, float)) or not 0 < sk <= per:
        return None
    ops = [o for o in m.get("openings") or [] if isinstance(o, dict)]
    return {"row": b["id"], "value": round(float(sk), 2), "unit": "m", "polygon": m.get("polygon") or poly,
            "perimeter_m": round(float(per), 2), "openings": [{"a": o.get("a"), "b": o.get("b"), "m": o.get("m")}
                                                            for o in ops][:40]}


_SUMMARY: dict = {}


def sheet_summary(path: str, page: int = 0, start_if_none: bool = False, visitor: str | None = None) -> dict:
    """summary() of the sheet's finished takeoff (with this visitor's own fixes), memoised by (job, result.json mtime,
    the visitor's fixes mtime, path); else its progress."""
    abs_path, virtual = resolve(path)
    key = job_key(abs_path, page)
    d = cache_dir() / key
    res = d / "result.json"
    if res.exists():
        fxp = _fixes_path(d, visitor)
        mk = (key, res.stat().st_mtime_ns, str(fxp.name), fxp.stat().st_mtime_ns if fxp.exists() else 0, virtual)
        if mk not in _SUMMARY:
            st = status(key, virtual, visitor)
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


def start(path: str, page: int = 0, refresh: bool = False, visitor: str | None = None) -> dict:
    """Start (or find) the takeoff of one sheet page; returns its status. One niced process per sheet; jobs queue on
    a machine-wide flock (CPU budget, COMMON.md)."""
    abs_path, virtual = resolve(path)
    key = job_key(abs_path, page)
    d = cache_dir() / key
    st = status(key, virtual, visitor) if d.exists() else {"status": "unknown"}
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
        visitor: str | None = None

    class FixReq(BaseModel):
        job: str
        id: str
        value: float | None = None
        unit: str | None = None
        polygon: list[list[float]] | None = None
        space: str | None = None
        item: str | None = None
        path: str = ""
        visitor: str | None = None

    class ForgetReq(BaseModel):
        visitor: str = ""
        keep: list[str] = []
        path: str = ""

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
            return start(req.path, req.page, req.refresh, req.visitor or None)
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
    def status_ep(job: str = Query(...), path: str = "", visitor: str = "") -> dict:
        _guard()
        try:
            return status(job, _virtual(path), visitor or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/fix")
    def fix_ep(req: FixReq) -> dict:
        """A person's measurement for a Review row (the panel's 'Measure'); value null = undo."""
        _guard()
        try:
            fix(req.job, req.id, req.value, req.unit, req.polygon, req.space, req.item, visitor=req.visitor or None)
            return status(req.job, _virtual(req.path), req.visitor or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/forget")
    def forget_ep(req: ForgetReq) -> dict:
        """'Start over': this visitor's Measure fixes go (except on the drawings in keep: their own uploads)."""
        _guard()
        try:
            return {"forgotten": forget(req.visitor, req.keep)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/summary")
    def summary_ep(path: str = Query(...), page: int = 0, start: int = 0, visitor: str = "") -> dict:
        """'This floor at a glance' for one sheet page (W5's card): the summary of its finished takeoff (cached:
        result.json + a hash of the file), else {status: none | queued | running, done, total}; start=1 starts the
        takeoff when there is none (the card can offer 'Generate takeoff')."""
        _guard()
        try:
            return sheet_summary(path, page, bool(start), visitor or None)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.get("/export")
    def export_ep(job: str = Query(...), fmt: str = "csv", path: str = "", visitor: str = ""):
        _guard()
        try:
            st = status(job, _virtual(path), visitor or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if st.get("status") not in ("done", "running"):
            raise HTTPException(409, f"takeoff is {st.get('status')}")
        extra = sum_rows(st) if st.get("status") == "done" else []
        return _file([*(st.get("rows") or []), *extra], fmt, st.get("file") or "")

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
