"""T3 — the uploaded project loop (audit REPORT-1951 risk #5 / fix #5; prompts/T3.md).

Several sheets uploaded together become ONE project: every sheet gets a role (floor plan, ceiling plan, section, ...)
and a level ('Level 01', 'Ground floor') read from its title block (VP's stoa.sheet.sets), else from the file name
(ISO 19650 names such as SEN-DFE-XX-01-D-X-0202 carry the level in field 4), else the user's own name for it.

A building-level question is answered by asking the pipeline on each sheet it concerns (the web asks /api/ask per
sheet, so private mode, caches and F7's corrections apply exactly as on a single sheet) and combining the readings
here: one sheet per level, never reflected ceiling plans / sections / elevations / partial plans of a level that has a
whole plan, never the same storey twice. When the readings cannot be summed safely there is NO total: the per-sheet
values are shown with the reason ("two plans of Level 01", "Level 02 gave no number").

Every answered quantity lands in the project's review list (source sheet, value, evidence, a 'checked' tick, the
user's corrected value, which also goes to F7's correction memory when that feature is on the tree); questions without
a usable answer are listed as open. The list exports as CSV (or XLSX through F2's writer when present).

Storage: one JSON file per project next to its uploads (<STOA_UPLOADS>/<id>/.stoa_project.json, the folder
/api/upload/set made), else under STOA_PROJECTS_DIR (default <tmp>/stoa-projects) for dataset files. Deterministic,
no model calls, $0.

Router: /feat/project (mounted when STOA_FEATURES lists 'project' or 'all'):
    POST   /feat/project/open                 {files: [path], name?}         -> project
    GET    /feat/project/{pid}                                               -> project
    POST   /feat/project/{pid}/plan           {question}                     -> which sheets to ask (and why not others)
    POST   /feat/project/{pid}/combine        {question, parts: [{sheet, answer}]} -> {answer, item}
    POST   /feat/project/{pid}/items          {question, file, page?, answer, scope?} -> item (a this-sheet answer)
    POST   /feat/project/{pid}/items/{iid}    {checked?, value?, unit?, note?, resolved?}   (also PATCH)
    POST   /feat/project/{pid}/items/{iid}/delete                            (also DELETE)
    POST   /feat/project/{pid}/sheet           {sheet, level?, role?, include?} (the user's names win over the reading)
    GET    /feat/project/{pid}/export?fmt=csv|xlsx (xlsx through F2's writer; CSV when it is absent)
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

VERSION = 4  # 3: levels from a plan line of the sheet text, Estonian levels (re-read once; items / user names kept)
_FILE = ".stoa_project.json"
_LOCK = threading.Lock()
MAX_PAGES = 160  # pages classified per PDF (~0.1 s per page: a 125-page US set in ~13 s; plans sit past page 40)
TOTAL_CAP = 0.8  # a sum over sheets is never 'sure' on its own
OPEN_BELOW = 0.3  # an answer less sure than this is listed as an open question (with its value), not as answered

ROLE_NAMES = {"floor_plan": "Floor plan", "rcp": "Ceiling plan", "section": "Section", "elevation": "Elevation",
              "schedule": "Schedule", "site_plan": "Site plan", "cover": "Cover sheet", "notes": "Notes",
              "detail": "Detail", "roof_plan": "Roof plan", "key_plan": "Key plan", "axon": "3D view",
              "drawing": "Drawing", "unknown": "Drawing"}
# kinds whose quantities are never added into a building total
NOT_SUMMED = {"rcp", "section", "elevation", "schedule", "site_plan", "cover", "notes", "detail", "key_plan", "axon",
              "roof_plan"}

_INDEX = re.compile(r"\b(?:INDEX\s+OF\s+DRAWINGS|DRAWING\s+INDEX|SHEET\s+INDEX|LIST\s+OF\s+DRAWINGS|DRAWING\s+LIST)\b", re.I)
_PARTIAL_LINE = re.compile(r"^\s*(?:.*\bAREA\s+PLAN|.*\bENLARGED\b.*\bPLAN|.*\bUNIT\s+PLAN|PARTIAL\b.*\bPLAN|.*\bPART\s+PLAN)\b",
                           re.I)
_UP = re.compile(r"^upload/([0-9a-f]{12})/(?:([a-z0-9_]+)/)?([^/\\]+)$")
# 'A0' in a DfE title block is the paper size and 'C01' / 'P02' the revision code (UK), not the sheet number
_PAPER = re.compile(r"^(?:A[0-4]|B[0-4]|ARCH\s?[A-E]|[CPT]\d{2})$", re.I)


# ---------------------------------------------------------------------------------------------------- paths and store
def uploads_root() -> Path:
    """Where the web middleware keeps uploads (web/mock/stoaApi.ts: STOA_UPLOADS, default <tmp>/stoa-uploads)."""
    return Path(os.environ.get("STOA_UPLOADS") or Path(tempfile.gettempdir()) / "stoa-uploads")


def projects_dir() -> Path:
    return Path(os.environ.get("STOA_PROJECTS_DIR") or Path(tempfile.gettempdir()) / "stoa-projects")


def resolve(path: str) -> str:
    """A path the browser knows (upload/<id>/<name>, upload/<id>/<style>/<name>, or dataset-relative) -> absolute file.
    Anything else raises ValueError (the API never reads arbitrary files)."""
    path = str(path or "").strip()
    if not path or ".." in Path(path).parts or path.startswith("/"):
        raise ValueError("path must be an upload (upload/<id>/<name>) or dataset-relative, without '..'")
    if path.startswith("upload/"):
        m = _UP.match(path)
        if not m:
            raise ValueError("not an upload path")
        root = uploads_root().resolve()
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
    return str(p)


def project_id(files: list[str]) -> str:
    """'<12 hex>' = the upload folder all files share (what /api/upload/set made, F7's project 'upload/<id>'), else
    'p<14 hex>' from the sorted file list."""
    ids = {m.group(1) for f in files if (m := _UP.match(f)) and not m.group(2)}
    if len(ids) == 1 and all(_UP.match(f) and not _UP.match(f).group(2) for f in files):
        return ids.pop()
    return "p" + hashlib.sha256("\n".join(sorted(files)).encode()).hexdigest()[:14]


def _check_pid(pid: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}|p[0-9a-f]{14}", str(pid or "")):
        raise ValueError("bad project id")
    return pid


def visitor_key(visitor: str | None) -> str:
    """A browser's own id (the web keeps a random one in localStorage 'stoa.visitor'); '' = the shared list (old
    clients, tests). QA 21:45 / 21:47: uploads are content-keyed, so without it every judge who drops the same sheets
    shared ONE review list and saw another visitor's corrections as 'corrected by you'."""
    v = str(visitor or "").strip()
    return v if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v) else ""


def store_path(pid: str, visitor: str = "") -> Path:
    _check_pid(pid)
    v = visitor_key(visitor)
    name = f".stoa_project.v-{v}.json" if v else _FILE
    if not pid.startswith("p"):
        d = uploads_root() / pid
        if d.is_dir():
            return d / name
    return projects_dir() / (f"{pid}.v-{v}.json" if v else f"{pid}.json")


def load(pid: str, visitor: str = "") -> dict | None:
    sp = store_path(pid, visitor)
    try:
        return json.loads(sp.read_text())
    except (OSError, ValueError):
        return None


def save(pid: str, data: dict, visitor: str = "") -> dict:
    sp = store_path(pid, visitor)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if visitor_key(visitor):
        data["visitor"] = visitor_key(visitor)
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["rev"] = int(data.get("rev", 0)) + 1
    tmp = sp.with_name(f".{sp.stem}.{os.getpid()}-{threading.get_ident()}.tmp")  # P1 19:59: per-thread temp names
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, sp)
    return data


# --------------------------------------------------------------------------------------------------------- levels
_ORD = {"basement": -1, "lower ground": -1, "ground": 0, "upper ground": 0, "first": 1, "second": 2, "third": 3,
        "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_LEVEL_NAME = re.compile(
    r"\b(?P<ord>basement|lower ground|upper ground|ground|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
    r"tenth|(?P<n1>\d{1,2})(?:st|nd|rd|th))\s+(?:floor|level|storey|story)\b|"
    r"\b(?:level|floor|storey|story|taso|etage|geschoss)\s*(?P<n2>-?\d{1,3})\b|"
    # Finnish '1. kerros' / '2. krs' / 'kerros 2' count the entrance floor as 1 (VP's stoa.sheet.sets says the same:
    # '1. KERROS' = storey 0); Swedish 'våning 1' likewise; German '1. OG' / 'Obergeschoss 2' count from 0
    # Estonian '1. korrus' / 'korrus 2' too (1 = the entrance floor; IFC storeys of the Esplanades model say so)
    r"\b(?P<fi>\d{1,2})\.?\s*(?:kerros|krs|våning\w*|vån|korrus)\b|\b(?:kerros|våning|korrus)\s*(?P<fi2>\d{1,2})\b|"
    r"\b(?P<n3>\d{1,2})\.?\s*(?:og|obergeschoss)\b|\bobergeschoss\s*(?P<n4>\d{1,2})\b|"
    r"\b(?P<roof>roof|mezzanine|vesikatto|vesikattotaso|katus)\b|"
    r"\b(?P<base>basement|kellari\w*|untergeschoss|kellergeschoss|källar\w*|souterrain|kelder|keldrikorrus)\b|"
    r"\b(?P<ground>pohjakerros|erdgeschoss|bottenvåning\w*|entréplan|markplan)\b|"
    r"\b(?P<attic>ullakko\w*|dachgeschoss|vindsplan|vind|katusekorrus|pööning\w*)\b|"
    r"\b(?P<ab>gf|lgf|ugf|ff|sf|tf|eg|ug|kg|dg|b[1-3]|l-?\d{1,2})\b", re.I)
_ORDS = "basement|ground|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\\d{1,2}(?:st|nd|rd|th)"
_AB = {"gf": 0, "ugf": 0, "lgf": -1, "ff": 1, "sf": 2, "tf": 3, "eg": 0, "ug": -1, "kg": -1, "b1": -1, "b2": -2, "b3": -3}


def level_name(key: str) -> str:
    """A storey key as a neutral name: '0' -> 'Level 00', 'roof' -> 'Roof', '-1' -> 'Basement -1'."""
    if key in ("roof", "mezzanine", "attic"):
        return key.capitalize()
    try:
        n = int(float(key))
    except (TypeError, ValueError):
        return str(key)
    return f"Level {n:02d}" if n >= 0 else f"Basement {n}"


def _iso_level_field(name: str) -> bool:
    """True when the file name is an ISO 19650 drawing number whose field 4 states the level ('SEN-DFE-XX-01-D-X-0202'):
    a code the office wrote, trusted before the sheet's printed lines; other names ('floor1') come after them."""
    parts = Path(str(name)).stem.split("-")
    return len(parts) >= 6 and len(parts[3]) == 2 and bool(re.fullmatch(r"\d\d|B\d|RF|R1|M1|M2", parts[3].upper()))


def level_from_name(name: str) -> tuple[str | None, str | None]:
    """The level a FILE NAME states: ISO 19650 'SEN-DFE-XX-01-D-X-0202' (field 4 = level: 00, 01, B1, RF; ZZ = several
    levels, XX = none), 'ground_floor', 'Level_02', '2nd-floor', 'L03', 'GF'. (key, display name) or (None, None)."""
    stem = Path(str(name)).stem
    parts = stem.split("-")
    if len(parts) >= 6 and len(parts[3]) == 2:
        f = parts[3].upper()
        if re.fullmatch(r"\d\d", f):
            return str(int(f)), f"Level {f}"
        if re.fullmatch(r"B\d", f):
            return str(-int(f[1])), f"Basement {f}"
        if f in ("RF", "R1"):
            return "roof", "Roof"
        if f in ("M1", "M2"):
            return "mezzanine", "Mezzanine"
    text = re.sub(r"[_\-.]+", " ", stem)
    if re.search(rf"\b(?:{_ORDS})\s+(?:and|&|\+|to)\s+(?:{_ORDS})\b", text, re.I) or re.search(
            r"\b\d{1,2}\.?\s*(?:ja|och|und|and|&|\+|-|–|\s)\s*\d{1,2}\.?\s*(?:kerros|krs|og|våning)", text, re.I):
        return None, None  # 'ground and first floor', '1. ja 2. kerros': one sheet, two levels
    hits = []
    for m in _LEVEL_NAME.finditer(text):
        if m.group("ord"):
            o = m.group("ord").lower()
            n = int(m.group("n1")) if m.group("n1") else _ORD.get(o)
            if n is None:
                continue
            hits.append((str(n), m.group(0).strip().capitalize()))
        elif m.group("n2") is not None:
            hits.append((str(int(m.group("n2"))), m.group(0).strip().capitalize()))
        elif m.group("fi") is not None or m.group("fi2") is not None:
            n = int(m.group("fi") if m.group("fi") is not None else m.group("fi2"))
            hits.append((str(max(n - 1, 0)) if n >= 1 else "0", m.group(0).strip()))
        elif m.group("n3") is not None or m.group("n4") is not None:
            hits.append((str(int(m.group("n3") if m.group("n3") is not None else m.group("n4"))), m.group(0).strip()))
        elif m.group("roof"):
            r = m.group("roof").lower()
            hits.append(("roof" if r.startswith(("roof", "vesikatto", "katus")) else r, m.group("roof").capitalize()))
        elif m.group("base"):
            hits.append(("-1", m.group("base").capitalize()))
        elif m.group("ground"):
            hits.append(("0", m.group("ground").capitalize()))
        elif m.group("attic"):
            hits.append(("attic", m.group("attic").capitalize()))
        elif m.group("ab") and m.group("ab").lower() == "dg":
            hits.append(("attic", "DG"))
        elif m.group("ab"):
            ab = m.group("ab").lower().replace("-", "")
            if ab.startswith("l") and ab[1:].isdigit():
                hits.append((str(int(ab[1:])), f"Level {int(ab[1:]):02d}"))
            elif ab in _AB:
                hits.append((str(_AB[ab]), m.group(0).upper()))
    keys = {k for k, _ in hits}
    if len(keys) == 1:
        return hits[0]
    return None, None  # none, or a name that states several levels ('ground and first floor')


_ROLE_WORDS = (("rcp", r"\b(rcp|reflected|ceiling)\b"), ("section", r"\bsections?\b|\bsect\b"),
               ("elevation", r"\belevations?\b|\belev\b"), ("site_plan", r"\bsite\b|\blandscape\b|\bmaster\s?plan\b"),
               ("schedule", r"\bschedules?\b"), ("detail", r"\bdetails?\b"), ("axon", r"\baxo\w*\b|\b3d\b|\bisometric\b"),
               ("roof_plan", r"\broof\s+plan\b"), ("cover", r"\bcover\b|\bindex\b|\bpattern\s+book\b"),
               ("floor_plan", r"\bplan\b|\bga\b|\blayout\b|\bfloor\b|\blevel\b|\bpohja\b|\bgrundriss\b"))


def role_from_name(name: str) -> str | None:
    text = re.sub(r"[_\-.]+", " ", Path(str(name)).stem).lower()
    for kind, rx in _ROLE_WORDS:
        if re.search(rx, text):
            return kind
    return None


# -------------------------------------------------------------------------------------------------------- sheets
def _image_layer(abs_path: str):
    """The image's text layer only if it was read before (the upload's warm-up reads it); never a cold 20-60 s read."""
    try:
        from stoa.solvers.raster.sheet import cached_layer

        return cached_layer(abs_path)
    except Exception:  # noqa: BLE001
        return None


def _scan_page(abs_path: str, page: int):
    """A SCANNED page (no text of its own) classified from what the upload warm-up already read (P1's
    stoa.formats.pages.classify_scan_page, night-P1; never a cold read). None when absent or not read yet."""
    try:
        from stoa.formats import pages as PG

        fn = getattr(PG, "classify_scan_page", None)
        return fn(abs_path, page) if fn else None
    except Exception:  # noqa: BLE001
        return None


def _page_is_picture(pg) -> bool:
    """A scanned page: one picture covers most of it (a CAD page with its text drawn as outlines is not one)."""
    try:
        area = abs(pg.rect.width * pg.rect.height) or 1.0
        return any(abs(_rect_area(i.get("bbox"))) >= 0.5 * area for i in pg.get_image_info())
    except Exception:  # noqa: BLE001
        return False


def _rect_area(b) -> float:
    x0, y0, x1, y1 = (float(v) for v in (b or (0, 0, 0, 0)))
    return (x1 - x0) * (y1 - y0)


_PLAN_LINE = re.compile(r"\b(?:plans?|plaan|planritning|pohjapiirros|pohjapiirustus|pohja|grundriss|layout)\b", re.I)
_NOT_THIS = re.compile(r"\b(?:see|refer|ref|for|per|on|as|typ\w*|continued|cont'?d|similar|enlarged|key\s*plan|part|partial|"
                       r"match\s*line|and|ja|och|und|to)\b|&|\+", re.I)
# in free text only a real level phrase names the sheet's level: never 'roof' ('STEEL TANK ROOF LAYOUT', 'ROOF DECK PER
# PLAN') nor a tag ('B2 BRIDGE CRANE PLAN'); 5 of 823 stoa-like sheets changed with the loose rule, all 5 wrong
_TEXT_LEVEL = re.compile(
    r"\b(?:basement|lower ground|upper ground|ground|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"\d{1,2}(?:st|nd|rd|th))\s+(?:floor|level|storey|story)\b|\b(?:level|floor|storey|story|taso|etage|geschoss)\s*-?\d{1,3}\b|"
    r"\b\d{1,2}\.?\s*(?:kerros|krs|våning\w*|korrus)\b|\b(?:kerros|våning|korrus)\s*\d{1,2}\b|"
    r"\b\d{1,2}\.?\s*(?:og|obergeschoss)\b|\bobergeschoss\s*\d{1,2}\b|\bbasement\s+(?:floor\s+)?plan\b|"
    r"\b(?:kellari\w*|untergeschoss|kellergeschoss|källar\w*|kelder|keldrikorrus|pohjakerros|erdgeschoss|bottenvåning\w*|"
    r"ullakko\w*|dachgeschoss|katusekorrus)\b", re.I)


def _level_from_text(text: str, with_line: bool = False):
    """The level one sheet's plan lines name ('Maleva tänav 18 (…) — Korruse plaan: 1.korrus' -> ('0', '1 korrus')):
    lines with a plan word and no cross-reference ('SEE FIRST FLOOR PLAN', 'GROUND AND FIRST FLOOR PLANS'); every
    line that names a level must name the same one, else none. with_line: (key, name, the first such line)."""
    keys: dict = {}
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not (4 <= len(ln) <= 160) or not _PLAN_LINE.search(ln) or not _TEXT_LEVEL.search(ln) or _NOT_THIS.search(ln):
            continue
        k, name = level_from_name(re.sub(r"[\s/\\:]+", "_", ln) + ".x")
        if k is not None:
            keys.setdefault(k, (name, ln))
    if len(keys) != 1:
        return (None, None, "") if with_line else (None, None)
    k, (name, ln) = next(iter(keys.items()))
    return (k, name, ln) if with_line else (k, name)


_SEG = re.compile(r"\s+[—–-]\s+|:\s+|\s+[·|]\s+")


def _plan_line_titles(sheets: list[dict]) -> str:
    """Sheets whose level came from a plan line of their own text ('Maleva tänav 18 korterelamu (…) — Korruse plaan:
    3.korrus'): an untitled one is titled by the line minus the part every such line shares and that names no plan /
    level ('Korruse plaan: 3.korrus'); that shared part is returned as the building's name ('Maleva tänav 18 korterelamu')."""
    own = [s for s in sheets if s.get("plan_line")]
    if not own:
        return ""
    segs = [[x.strip() for x in _SEG.split(s["plan_line"]) if x.strip()] for s in own]
    common = [x for x in segs[0] if len(own) >= 2 and len(x) >= 6 and all(x in o for o in segs[1:])
              and not _PLAN_LINE.search(x) and level_from_name(re.sub(r"\s+", "_", x) + ".x")[0] is None]
    for s, sg in zip(own, segs):
        if not s.get("title"):
            rest = [x for x in sg if x not in common]
            s["title"] = (": ".join(rest) if rest and common else s["plan_line"])[:80]
    if not common:
        return ""
    short = re.sub(r"\s*\([^)]*\)\s*", " ", common[0]).strip()
    return (short if len(short) >= 6 else common[0])[:60]


def classify_file(virtual: str, abs_path: str | None = None) -> list[dict]:
    """One uploaded file -> its sheets (one per PDF page, one per image) as plain dicts."""
    abs_path = abs_path or resolve(virtual)
    low = abs_path.lower()
    raw, media, texts = [], {}, []
    try:
        from stoa.sheet.sets import classify

        if low.endswith(".pdf"):
            import pymupdf

            with pymupdf.open(abs_path) as d:
                n = min(len(d), MAX_PAGES)
                texts = [d[i].get_text() for i in range(n)]
                pictured = [_page_is_picture(d[i]) for i in range(n)]
            raw = [classify(abs_path, i) for i in range(n)]
            for i, t in enumerate(texts):
                media[i] = "scan" if len(t.strip()) < 20 and pictured[i] else "vector"
                if raw[i].storey is None and len(t.strip()) < 20:
                    raw[i] = _scan_page(abs_path, i) or raw[i]
            for s, t in zip(raw, texts):
                if _INDEX.search(t):  # a cover / index lists every other sheet's title: never a plan of its own
                    s.kind, s.storey, s.storey_name, s.title = "cover", None, None, "Cover sheet / drawing index"
                elif s.kind == "floor_plan" and not s.partial and any(
                        _PARTIAL_LINE.search(ln) for ln in t.splitlines() if len(ln.strip()) <= 40):
                    s.partial = True  # 'SECOND FLOOR COMMON AREA PLAN', 'ENLARGED PLAN': a part of the level
        elif low.endswith((".png", ".jpg", ".jpeg")):
            raw = [classify(abs_path, 0, layer=_image_layer(abs_path))]
    except Exception:  # noqa: BLE001 - an unreadable file still becomes one 'Drawing' sheet
        raw = []
    if not raw and low.endswith((".png", ".jpg", ".jpeg", ".pdf")):
        from types import SimpleNamespace

        raw = [SimpleNamespace(page=0, number=None, title=None, kind="unknown", storey=None, storey_name=None,
                               building=None, partial=False, ratio=None, page_ratio=None, confidence=0.3)]
    out = []
    one = len(raw) == 1
    # a 'title' on 3+ pages of one file is a repeated title-block / legend line ('SPOT ELEVATION MARKER' on 14 plans of
    # fs_proto_hq, 'Revision Schedule'), not the sheet's own title: the title rules below never judge a sheet by it
    seen: dict = {}
    for s in raw:
        t = re.sub(r"\s+", " ", (s.title or "").strip().upper())
        seen[t] = seen.get(t, 0) + 1
    for s in raw:
        own_title = bool((s.title or "").strip()) and seen.get(re.sub(r"\s+", " ", s.title.strip().upper()), 0) < 3
        number = s.number if s.number and not _PAPER.match(s.number) else None
        kind, kind_src = s.kind or "unknown", "title block"
        if kind in ("key_plan", "drawing") and one and role_from_name(virtual) == "site_plan":
            kind, kind_src = "site_plan", "file name"  # 'Primary_school_landscape_master_plan.pdf' read as a key plan
        if kind in ("unknown", "drawing"):
            guess = role_from_name(virtual) if one else None
            if guess:
                kind, kind_src = guess, "file name"
            elif kind == "unknown":  # a vector page with text WAS read (its kind is just unknown): not 'not read'
                pg = int(s.page)
                kind_src = ("not recognised" if media.get(pg) == "vector" and pg < len(texts) and
                            len(texts[pg].strip()) >= 20 else "not read")
        storey, level, level_src, plan_line = s.storey, s.storey_name, "title block", ""
        if storey is None and (s.title or "").strip():  # 'POHJAPIIRUSTUS 2. KRS', 'Grundriss Obergeschoss 2'
            storey, level = level_from_name(re.sub(r"[\s/\\]+", "_", s.title.strip()) + ".x")
        fn_storey, fn_level = level_from_name(virtual) if storey is None and one else (None, None)
        if storey is None and fn_storey is not None and _iso_level_field(virtual):
            storey, level, level_src = fn_storey, fn_level, "file name"  # ISO 19650 field 4: the office's own code
        if storey is None and kind in ("floor_plan", "drawing", "unknown") and int(s.page) < len(texts):
            # no title block level: a plan line of the sheet's own text ('Korruse plaan: 1.korrus', 'GROUND FLOOR PLAN'
            # printed outside the block), only when every such line names the same level (a file renamed on upload)
            storey, level, plan_line = _level_from_text(texts[int(s.page)], with_line=True)
            level_src = "sheet text" if storey is not None else ""
            if storey is not None and kind in ("drawing", "unknown"):
                kind, kind_src = "floor_plan", "sheet text"  # its own line says '<level> plan'
        if storey is None and fn_storey is not None:
            # a name the uploader typed ('floor1.pdf', 'L03', 'ground_floor') only when the sheet prints no level of
            # its own: 'floor1.pdf' of a sheet printing 'Korruse plaan: 1.korrus' is the ground floor (storey 0)
            storey, level, level_src = fn_storey, fn_level, "file name"
        if kind == "floor_plan" and own_title and re.search(r"reflected\s+ceiling|\brcp\b|\bceiling\s+plan", s.title, re.I) and \
                not re.search(r"\bfloor\s+plan\s*(?:&|and|\+)", s.title or "", re.I):
            kind, kind_src = "rcp", "sheet title"  # 'REFLECTED CEILING PLAN - SECOND FLOOR' (FTC A-122 read as a plan)
        if kind == "floor_plan" and kind_src == "title block" and own_title and _NOT_A_PLAN_TITLE.search(s.title) and \
                not re.match(r"\s*(?:and|&|\+|,)", s.title, re.I) and \
                not _PLAN_WORD.search(s.title or ""):
            # 'ROOM FINISH SCHEDULE', 'PLUMBING DETAILS', 'A7 WALL SECTION @ LOW ROOF', 'FIRE ALARM MATRIX': never a plan
            kind, kind_src = role_from_name(re.sub(r"\s+", "_", s.title.strip()) + ".x") or "detail", "sheet title"
            if kind == "floor_plan":
                kind = "detail"
        if storey is not None and not level:
            level = level_name(storey)
        if level and level.isupper():  # 'FIRST FLOOR' -> 'First floor', 'LEVEL 01' -> 'Level 01'
            level = level[:1] + level[1:].lower()
        if kind == "cover" and not s.title:
            s.title = "Cover sheet / drawing index"
        title = (s.title or "").strip()
        out.append({
            "id": f"{virtual}#{int(s.page)}", "file": virtual, "page": int(s.page), "number": number or "",
            "title": title, "kind": kind, "role": ROLE_NAMES.get(kind, kind.replace("_", " ").capitalize()),
            "kind_source": kind_src, "storey": storey, "level": level or "", "level_source": level_src if level else "",
            "building": s.building or "", "partial": bool(s.partial), "ratio": s.ratio, "page_ratio": s.page_ratio,
            "confidence": float(s.confidence or 0), "user": {},
            "medium": media.get(int(s.page), "image" if low.endswith((".png", ".jpg", ".jpeg")) else "vector"),
            "plan_line": plan_line,
        })
    return out


def _label(sh: dict) -> str:
    """'A-102 · Secondary School GA Plan Level 01' (the file name when nothing is printed), '(page 3)' on a set."""
    head = " · ".join(x for x in (sh.get("number"), sh.get("title")) if x) or Path(sh["file"]).name
    if len(head) > 70:
        head = head[:69] + "…"
    if sh.get("copy_of") and (sh.get("number") or sh.get("title")):  # two sheets with one title: say which is the copy
        head += f" · copy ({Path(sh['file']).name})"
    return head + (f" (page {sh['page'] + 1})" if int(sh.get("_pages", 1)) > 1 else "")


def effective(sh: dict) -> dict:
    """The sheet as the project uses it: the user's level / role / include override the reading."""
    u = sh.get("user") or {}
    e = dict(sh)
    if u.get("level"):
        e["level"], e["level_source"] = u["level"], "you"
        k, _ = level_from_name(u["level"].replace(" ", "_"))
        e["storey"] = k if k is not None else "u:" + re.sub(r"\s+", " ", u["level"].strip().lower())
    if u.get("building"):
        e["building"], e["building_source"] = u["building"], "you"
    if u.get("role"):
        inv = {v.lower(): k for k, v in ROLE_NAMES.items()}
        e["kind"] = inv.get(str(u["role"]).lower(), u["role"])
        e["role"], e["kind_source"] = ROLE_NAMES.get(e["kind"], str(u["role"])), "you"
    e["include"] = u.get("include")
    e["label"] = _label(e)
    return e


OTHER_SHEETS = "Other sheets"


def effective_all(sheets: list[dict]) -> list[dict]:
    """Every sheet as the project uses it. A visitor who named the building of SOME sheets by hand (the reader found
    one building) splits the project: the sheets they left unnamed become 'Other sheets'."""
    es = [effective(s) for s in sheets]
    named = {e["building"] for e in es if e.get("building")}
    if len(named) == 1 and any(e.get("building_source") == "you" for e in es):
        for e in es:
            if not e.get("building"):
                e["building"], e["building_source"] = OTHER_SHEETS, "the rest (you named one building)"
    return es


def build(files: list[str], name: str = "", old: dict | None = None) -> dict:
    """A project from its files; keeps the review list, the user's sheet names and the questions of `old`."""
    sheets, unreadable, abs_of = [], [], {}
    for f in files:
        try:
            abs_path = resolve(f)
        except (ValueError, FileNotFoundError) as e:
            unreadable.append(f"{f}: {e}")
            continue
        if not abs_path.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
            continue  # an IFC / DXF of the set: linked by the web, not a sheet here
        got = classify_file(f, abs_path)
        for s in got:
            s["_pages"] = len(got)
            abs_of[s["id"]] = abs_path
        sheets.extend(got)
    if not sheets:
        raise ValueError("none of these files is a readable drawing sheet (PDF / PNG / JPG upload): "
                         + "; ".join(unreadable)[:300])
    pl_name = _plan_line_titles(sheets)
    find_copies(sheets, abs_of)
    split_buildings(sheets, abs_of)
    users = {s["id"]: s.get("user") or {} for s in (old or {}).get("sheets", [])}
    for s in sheets:
        s["user"] = users.get(s["id"], {})
    old_name = (old or {}).get("name") if (old or {}).get("name_user") else ""
    several = len({s.get("building") for s in sheets if s.get("building")}) >= 2
    proj = {"version": VERSION, "id": project_id(files),
            "name": name or old_name or (pl_name if pl_name and not several else _guess_name(sheets, files)),
            "name_user": bool(name or old_name),
            "files": list(files), "sheets": sheets, "items": list((old or {}).get("items", [])),
            "created": (old or {}).get("created") or time.strftime("%Y-%m-%dT%H:%M:%S"), "rev": (old or {}).get("rev", 0)}
    return proj


# ------------------------------------------------------------------------------------------------------------ copies
# A building's sheet uploaded twice — the CAD PDF and a scan / photo / picture of the same print — is ONE sheet: its
# quantities are counted once (from the copy with its own text), and the copy takes the original's level when it has
# none of its own. Two rules: (1) the same printed drawing number (OCR-tolerant: one character off on >= 8); (2) for a
# picture / scanned page with no number read yet (the upload's OCR has not landed), the drawing's LOOK: ink-density
# thumbnails (64 x 64, page shading flattened, page edge masked, +-3 cell shift) correlate >= 0.95 with ONE plan and
# >= 0.08 better than with any other. Measured (scratch, 21:20): X1's scans of DfE SEN Level 00 vs SEN L00 / L01 =
# 0.97-0.98 / 0.63-0.69; EFA type-3 GF / 1F / 2F scans vs their own floor 0.87-0.93 but vs another floor up to 0.84,
# and two typical floors' own CAD sheets 0.89-0.94 — so the look alone only decides at >= 0.95 (EFA scans wait for
# their printed number), and it never overrides a level read from the copy itself.
COPY_LOOK, COPY_MARGIN, COPY_MAX = 0.95, 0.08, 24
COPY_LOOK_PICS = 0.97  # two pictures: 24 different HAER HI-76 sheets are up to 0.943 alike; a JPEG vs TIFF of one is 0.99
_PLANLIKE = ("floor_plan", "drawing", "unknown")


def _num_key(n: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(n or "").upper()).replace("O", "0").replace("I", "1").replace("L", "1")


def _near(a: str, b: str) -> bool:
    if a == b:
        return bool(a)
    if len(a) < 8 or len(a) != len(b):
        return False
    return sum(x != y for x, y in zip(a, b)) <= 1


def _thumb(abs_path: str, page: int = 0, n: int = 64):
    """The drawing's ink density as an n x n grid (page shading flattened, page edge masked, cropped to the ink)."""
    import numpy as np
    from PIL import Image, ImageFilter

    Image.MAX_IMAGE_PIXELS = None
    side = 1000
    if abs_path.lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(abs_path) as d:
            pg = d[int(page)]
            z = side / max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY)
            im = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    else:
        im = Image.open(abs_path)
        im.draft("L", (side, side))
        im = im.convert("L")
        im.thumbnail((side, side))
    a = np.asarray(im, dtype=np.float32)
    bg = np.asarray(im.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(20)), dtype=np.float32)
    d = np.clip(bg - a - 12, 0, 255)
    m = int(0.025 * max(d.shape))
    d[:m], d[-m:], d[:, :m], d[:, -m:] = 0, 0, 0, 0
    ys, xs = np.where(d > 30)
    if len(xs) < 50:
        return None
    x0, x1 = np.quantile(xs, [0.01, 0.99])
    y0, y1 = np.quantile(ys, [0.01, 0.99])
    d = d[int(y0):int(y1) + 1, int(x0):int(x1) + 1]
    t = Image.fromarray(d.astype(np.uint8)).resize((n, n), Image.BOX).filter(ImageFilter.GaussianBlur(1.2))
    return np.asarray(t, dtype=np.float32)


def _alike(a, b, shift: int = 3) -> float:
    import numpy as np

    n = a.shape[0]
    best = -1.0
    for dy in range(-shift, shift + 1):
        for dx in range(-shift, shift + 1):
            A = a[max(0, dy):n + min(0, dy), max(0, dx):n + min(0, dx)]
            B = b[max(0, -dy):n + min(0, -dy), max(0, -dx):n + min(0, -dx)]
            A = (A - A.mean()) / (A.std() + 1e-6)
            B = (B - B.mean()) / (B.std() + 1e-6)
            best = max(best, float((A * B).mean()))
    return best


_WORDS: dict = {}


def _page_words(abs_path: str, page: int = 0) -> dict:
    """A PDF page's words with their counts (cached by size + mtime); {} for pictures / unreadable pages."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return {}
    key = (abs_path, int(page), st.st_size, int(st.st_mtime))
    if key not in _WORDS:
        from collections import Counter

        try:
            import pymupdf

            with pymupdf.open(abs_path) as d:
                _WORDS[key] = dict(Counter(_WORD.findall(d[int(page)].get_text().lower())))
        except Exception:  # noqa: BLE001
            _WORDS[key] = {}
    return _WORDS[key]


def _same_words(a: str, pa: int, b: str, pb: int, need: float = 0.95) -> bool:
    """Two pages print (nearly) the same words: weighted Jaccard >= need."""
    wa, wb = _page_words(a, pa), _page_words(b, pb)
    if not wa or not wb:
        return False
    keys = set(wa) | set(wb)
    inter = sum(min(wa.get(k, 0), wb.get(k, 0)) for k in keys)
    union = sum(max(wa.get(k, 0), wb.get(k, 0)) for k in keys)
    return union > 0 and inter / union >= need


def find_copies(sheets: list[dict], abs_of: dict | None = None) -> None:
    """Mark a sheet that is another copy of an uploaded sheet: s['copy_of'] = the original's id, s['copy_how'] = why.
    In place; a copy without a level of its own takes the original's (level_source 'copy of …')."""
    order = {s["id"]: i for i, s in enumerate(sheets)}
    for s in sheets:
        for k in ("copy_of", "copy_how", "copy_score", "title_read"):
            s.pop(k, None)
    import difflib

    def tnorm(t: str) -> str:  # OCR's usual confusions folded the same way on both sides (O/0, l/I/1, S/5)
        t = re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()
        return t.translate(str.maketrans({"o": "0", "l": "1", "i": "1", "s": "5"}))

    def other_title(a: dict, b: dict, need: float = 0.6) -> bool:  # both titles read and clearly different drawings
        ta, tb = tnorm(a.get("title")), tnorm(b.get("title"))
        return len(ta) >= 8 and len(tb) >= 8 and difflib.SequenceMatcher(None, ta, tb).ratio() < need

    def ocr_read(x: dict) -> bool:  # its number came from OCR (a picture / scanned page), not from the PDF's text
        return x.get("medium", "vector") != "vector"

    # (1) the same printed drawing number in two files (not a job / record number every sheet prints in that slot:
    # HAER HI-76 scans C3 and C6 both read 'HAER No. HI-76' there; F8 22:27) -> their titles must not disagree
    keyed = [(s, _num_key(s.get("number"))) for s in sheets if _num_key(s.get("number"))]
    shared = {k for k in {k for _, k in keyed} if sum(1 for _, kk in keyed if kk == k) >= 4}
    for i, (s, k) in enumerate(keyed):
        if s.get("copy_of") or k in shared:  # one number on 4+ sheets: the set's job / record number (HAER HI-76)
            continue
        # one character off only where OCR read a number (a picture / scan): two CAD sheets 'SEC-DFE-XX-00-…' and
        # 'SEN-DFE-XX-00-…' are two schools' plans (T3 cycle 4: the secondary school was 'a copy' of the special one)
        group = [s] + [t for t, kt in keyed[i + 1:] if t["file"] != s["file"] and not t.get("copy_of") and (
            kt == k or (_near(k, kt) and (ocr_read(s) or ocr_read(t))))]
        if len(group) < 2:
            continue
        group.sort(key=lambda g: (g.get("medium") != "vector", order[g["id"]]))
        orig = group[0]
        for g in group[1:]:
            if g["storey"] is not None and orig["storey"] is not None and g["storey"] != orig["storey"]:
                continue  # one number, two levels read: not a copy (a typo in a title block?)
            if other_title(g, orig, 0.6 if _num_key(g.get("number")) == _num_key(orig.get("number")) else 0.85):
                continue  # one number, two different titles: a number printed on every sheet of the set
            g["copy_of"], g["copy_how"] = orig["id"], f"the same drawing number, {orig['number']}"
    # (2) a picture / scanned page with the same level AND (OCR-tolerant) title as a CAD sheet
    for sc in sheets:
        if sc.get("copy_of") or sc.get("medium") not in ("image", "scan") or sc["storey"] is None or len(tnorm(sc.get("title"))) < 8:
            continue
        same = [c for c in sheets if c.get("medium") == "vector" and c["file"] != sc["file"] and c["storey"] == sc["storey"]
                and len(tnorm(c.get("title"))) >= 8
                and difflib.SequenceMatcher(None, tnorm(sc["title"]), tnorm(c["title"])).ratio() >= 0.88]
        if len(same) == 1:
            sc["copy_of"], sc["copy_how"] = same[0]["id"], "the same title and level"
    # (3) a picture / scanned page whose number is not read yet: the drawing's look
    scans = [s for s in sheets if s.get("medium") in ("image", "scan") and not s.get("copy_of") and not s.get("number")
             and s["kind"] in _PLANLIKE][:12]
    cands = [s for s in sheets if s.get("medium") == "vector" and s["kind"] in _PLANLIKE][:COPY_MAX]
    thumbs: dict = {}

    def th(s):
        if s["id"] not in thumbs:
            try:
                thumbs[s["id"]] = _thumb(abs_of[s["id"]], s["page"])
            except Exception:  # noqa: BLE001 - an unreadable picture is simply not matched
                thumbs[s["id"]] = None
        return thumbs[s["id"]]

    # pictures of one sheet with no CAD file (a JPEG and a TIFF of one HAER scan; a phone photo + a scan): the
    # later picture is the copy, by the same look rule
    pics = [s for s in sheets if s.get("medium") in ("image", "scan") and s["kind"] in _PLANLIKE][:COPY_MAX]
    if scans and (cands or len(pics) >= 2) and abs_of:
        for sc in scans:
            t = th(sc)
            if t is None or sc.get("copy_of"):
                continue
            pool = cands + [p for p in pics if p is not sc and p["file"] != sc["file"] and not p.get("copy_of")
                            and order[p["id"]] < order[sc["id"]]]
            pool += [p for p in pics if p is not sc and p["file"] != sc["file"] and not p.get("copy_of")
                     and order[p["id"]] > order[sc["id"]] and p not in pool]  # a later picture: scored, never the original
            scored = sorted(((_alike(t, th(c)), j, c) for j, c in enumerate(pool) if th(c) is not None),
                            key=lambda x: (-x[0], x[1]))
            if not scored:
                continue
            best, _, c = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            if best < COPY_LOOK or best - second < COPY_MARGIN:
                continue
            if c.get("medium") in ("image", "scan") and (order[c["id"]] > order[sc["id"]] or best < COPY_LOOK_PICS):
                continue  # its twin comes later (marked when its turn comes); two pictures need 0.97 (HAER: 0.943 apart)
            if sc["storey"] is not None and c["storey"] is not None and sc["storey"] != c["storey"]:
                continue
            sc["copy_of"] = c["id"]
            sc["copy_how"] = f"the drawing is {best:.0%} alike"
            sc["copy_score"] = round(best, 3)
    # (4) two CAD prints of one drawing (EFA 'x.pdf' + 'x.paperA1.pdf'; an A1 and an A3 PDF): no number read on either,
    # the same level, the same title, and the same look -> the second one is a copy (counted once, from the first)
    vec = [s for s in sheets if s.get("medium", "vector") == "vector" and not s.get("copy_of") and s["storey"] is not None
           and s["kind"] in _PLANLIKE and not _num_key(s.get("number")) and len(tnorm(s.get("title"))) >= 8][:COPY_MAX]
    if abs_of and len(vec) >= 2:
        for i, a in enumerate(vec):
            if a.get("copy_of"):
                continue
            for b in vec[i + 1:]:
                if b.get("copy_of") or b["file"] == a["file"] or b["storey"] != a["storey"]:
                    continue
                if difflib.SequenceMatcher(None, tnorm(a["title"]), tnorm(b["title"])).ratio() < 0.95:
                    continue
                ta, tb = th(a), th(b)
                score = _alike(ta, tb) if ta is not None and tb is not None else 0.0
                # a reprint carries the same words; two houses of one design built twice differ in their title
                # blocks (project, address): their plans are both counted
                if score >= COPY_LOOK and _same_words(abs_of[a["id"]], a["page"], abs_of[b["id"]], b["page"]):
                    b["copy_of"], b["copy_score"] = a["id"], round(score, 3)
                    b["copy_how"] = f"the same title and level, and the drawing is {score:.0%} alike"
    by_id = {s["id"]: s for s in sheets}
    for s in sheets:
        o = by_id.get(s.get("copy_of") or "")
        if o is None:
            continue
        src = f"sheet it copies ({o.get('number') or Path(o['file']).name})"
        if s["storey"] is None and o["storey"] is not None:
            s["storey"], s["level"], s["level_source"] = o["storey"], o["level"], src
        elif s["storey"] == o["storey"] and s.get("level_source") == "file name" and o.get("level"):
            s["level"] = o["level"]  # 'Level00' from 'plan-level00-scan200.pdf' -> the title block's 'Level 00'
            s["building"] = s.get("building") or o.get("building", "")
        if s["kind"] in ("unknown", "drawing") and o["kind"] not in ("unknown", "drawing"):
            s["kind"], s["role"], s["kind_source"] = o["kind"], o["role"], src
        if s.get("medium") in ("image", "scan") and o.get("title"):
            # a scan's title is what OCR read ('Special Schoo! Plan Level 00'): show the original's (the label adds 'copy')
            s["title_read"], s["title"] = s.get("title", ""), o["title"]


# --------------------------------------------------------------------------------------------------------- buildings
# Several buildings in one upload (Stoa: 'about 8 floor plans per building, several buildings'). VP's reader names a
# building only when a title prints one ('BUILDING B'), so two schools dropped together became ONE 'School' whose
# Level 00 had two plans (and a 'whole building' count would have asked for 'which one?'). A set's own identity
# tells them apart, at $0: (1) the ISO 19650 drawing number's project field ('SEN-DFE-XX-00-D-X-0201' vs
# 'PRI-DFE-XX-00-…'); (2) what the titles say besides their level / plan words ('Special School' vs 'Primary
# School'), used only when it explains a clash: whole floor plans of ONE level in two groups. A group without a floor
# plan (a site plan with its own code) is never a building of its own; a copy goes with its original; a sheet
# nothing places stays '' (unplaced: left out of one building's totals, with the reason).
_ISO_NO = re.compile(r"^([A-Z0-9]{2,6})-[A-Z0-9]{2,6}-[A-Z0-9]{1,3}-[A-Z0-9]{1,3}-[A-Z0-9]{1,3}-", re.I)
_TITLE_NOISE = re.compile(
    r"\b(?:level|floor|storey|story)\s*-?\d{1,3}\b|\b(?:ga|general|arrangement|plans?|layouts?|floors?|levels?|storeys?|"
    r"stor(?:y|ies)|ground|first|second|third|fourth|fifth|sixth|seventh|eighth|basement|lower|upper|roof|mezzanine|"
    r"proposed|existing|overall|part|partial|enlarged|areas?|zones?|wings?|north|south|east|west|key|sheet|drawing|dwg|"
    r"reflected|ceiling|rcp|furniture|finishes|setting|out|copy|scan|scanned|[a-z]|\d+)\b"
    # the kind of plan, not the building: one level is often drawn as a GA, a fire strategy, a furniture layout …
    r"|\b(?:fire|strategy|escape|means|compartment\w*|accessib\w*|access|demolition|drainage|electrical|lighting|power|"
    r"mechanical|plumbing|sanitary|structural|framing|slab|foundations?|landscape|landscaping|ventilation|heating|hvac|"
    r"sprinklers?|security|signage|wayfinding|flooring|floor\s*finish\w*|equipment|doors?|windows?|partitions?|walls?|"
    r"types?|zoning|data|services|coordination|setting-out|dimension\w*|setout|tiling|joinery|ffe|ff&e|room|rooms|"
    r"schedule|key|location|existing|demo|new|works|scope|phase|phasing|stage|issue|revision|rev|preliminary|tender|"
    r"construction|planning|concept|scheme|design|sketch|option|options|typical"
    # Finnish / Swedish / German plan and level words ('POHJAPIIRUSTUS 2. KRS', 'Grundriss 1. OG', 'Plan 2 våning')
    r"|pohjapiirustus|pohjapiirros|pohja|kerros|kerrokset|krs|kellari\w*|ullakko|vesikatto|pohjakerros|"
    r"grundriss|geschoss|erdgeschoss|obergeschoss|untergeschoss|dachgeschoss|og|eg|ug|"
    r"planritning|ritning|våning\w*|vån|källare|vind|bottenvåning)\b", re.I)
# a building's own id in a title ('TALO A', 'BUILDING 3305', 'Haus 2'); a wing / staircase is a part of one building
_BUILDING_ID = re.compile(r"\b(talo|rakennus|building|bldg|block|blokki|haus|hus|house|byggnad)"
                          r"\s*[-.:]?\s*([A-Z]|\d{1,5}|[A-Z]\d{1,3}|\d{1,3}[A-Z])\b", re.I)


def _title_key(title: str) -> str:
    """What a sheet title names besides its level and plan words: 'Primary School GA Plan Level 00' -> 'primary school';
    'FIRST FLOOR PLAN - AREA A' -> ''."""
    ids = [f"{w.lower()} {i.upper()}" for w, i in _BUILDING_ID.findall(str(title or ""))]  # 'TALO A' keeps its letter
    t = _BUILDING_ID.sub(" ", str(title or ""))
    t = _TITLE_NOISE.sub(" ", re.sub(r"[^\w]+|_", " ", t))
    return " ".join([w for w in t.lower().split() if len(w) >= 3] + ids)


_BLOCK_LABELS = set("""drawn checked approved date scale sheet project title revision rev client architect engineer designer
designed drawing number status issue purpose notes note north legend copyright description revisions issued construction
preliminary tender information only dimensions unless otherwise stated noted metres millimetres size paper original
suunnittelija piirtäjä tarkastanut hyväksynyt päiväys mittakaava piirustus numero muutos sisältö rakennuskohde tilaaja
pääsuunnittelija arkkitehti työnumero""".split())
_ROOMISH = re.compile(r"\b(?:room|office|wc|toilets?|store|stores|storage|corridor|kitchen|hall|classroom|stairs?|lift|"
                      r"plant|lobby|entrance|reception|bedroom|bath|living|dining|garage|closet|utility|shower|changing|"
                      r"meeting|staff|laundry|cleaner|kitchenette|huone|keittiö|wc|pesuhuone|sauna|eteinen|olohuone|"
                      r"makuuhuone|varasto|porras|hissi|aula|toimisto)\b", re.I)
_BLOCK: dict = {}


def _block_lines(abs_path: str, page: int = 0) -> set[str]:
    """A vector page's title-block lines: text in the outer right strip (x > 80 %) or bottom strip (y > 88 %), 6-60
    characters, with a word of 4+ letters that is not a title-block label / level / plan word, and not a room tag.
    The project name, client and address of a set are among them; they differ between two buildings' sets."""
    try:
        st = os.stat(abs_path)
    except OSError:
        return set()
    key = (abs_path, int(page), st.st_size, int(st.st_mtime))
    if key in _BLOCK:
        return _BLOCK[key]
    out: set[str] = set()
    try:
        import pymupdf

        with pymupdf.open(abs_path) as d:
            pg = d[int(page)]
            W, H = pg.rect.width, pg.rect.height
            for b in pg.get_text("dict").get("blocks", []):
                for ln in b.get("lines", []):
                    x0, y0, _, _ = ln["bbox"]
                    if not (x0 > 0.80 * W or y0 > 0.88 * H):
                        continue
                    t = "".join(sp.get("text", "") for sp in ln.get("spans", [])).strip()
                    n = " ".join(_WORD.findall(t.lower()))
                    if not 6 <= len(n) <= 60 or _ROOMISH.search(n):
                        continue
                    core = _TITLE_NOISE.sub(" ", n).split()
                    if any(len(w) >= 4 and w not in _BLOCK_LABELS and not w.isdigit() for w in core):
                        out.add(n)
    except Exception:  # noqa: BLE001 - an unreadable page has no title-block lines
        out = set()
    _BLOCK[key] = out
    return out


def _block_groups(own: list[dict], abs_of: dict, whole_plan) -> tuple[dict, dict]:
    """Several buildings' sets with plain titles ('FIRST FLOOR PLAN') and no ISO numbers: when one level is drawn
    twice, group the CAD sheets by the title-block lines they share (2+). Split when the groups explain a level drawn
    twice (its plans fall in different groups); a group may still draw a level twice itself (two variants of one
    design: answered 'which one?' inside that building). -> ({key: [sheets]}, {key: name}) or ({}, {})."""
    vec = [s for s in own if s.get("medium", "vector") == "vector" and abs_of.get(s["id"])][:60]
    per: dict = {}
    for s in vec:
        if whole_plan(s) and s["storey"] is not None:
            per.setdefault(s["storey"], []).append(s)
    clashes = [ss for ss in per.values() if len(ss) >= 2]
    if not clashes:
        return {}, {}
    lines = {s["id"]: _block_lines(abs_of[s["id"]], s["page"]) for s in vec}

    def linked(a: dict, b: dict) -> bool:
        n = len(lines[a["id"]] & lines[b["id"]])
        # 2+ shared lines AND half the smaller block: one publisher's / firm's lines alone (EFA: 'Education
        # Funding Agency', 'EFA baseline school designs', 'Overall GIFA' = 3 of 8) do not make one building
        return n >= 2 and n >= 0.5 * min(len(lines[a["id"]]), len(lines[b["id"]]))

    # the floor plans make the buildings; a section / axo with only the publisher's lines would bridge them all
    plans = [s for s in vec if whole_plan(s)]
    parent = {s["id"]: s["id"] for s in plans}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(plans):
        for b in plans[i + 1:]:
            if linked(a, b):
                parent[find(a["id"])] = find(b["id"])
    comps: dict = {}
    for s in plans:
        comps.setdefault(find(s["id"]), []).append(s)
    for s in vec:  # another sheet joins the one group whose plans it links to (none / several: unplaced)
        if s["id"] in parent:
            continue
        hit = {k for k, g in comps.items() if any(linked(s, p) for p in g)}
        if len(hit) == 1:
            comps[hit.pop()].append(s)
    with_plans = {k: g for k, g in comps.items()  # a building: whole plans of 2+ levels
                  if len({s["storey"] for s in g if whole_plan(s) and s["storey"] is not None}) >= 2}
    if len(with_plans) < 2:
        return {}, {}
    if not any(len({find(s["id"]) for s in ss}) >= 2 for ss in clashes):
        return {}, {}  # every level drawn twice is drawn twice inside one group: the groups explain nothing
    names: dict = {}
    keys_of = {k: [x for x in (_title_key(s.get("title") or "") for s in g) if x] for k, g in with_plans.items()}
    for i, (k, g) in enumerate(sorted(with_plans.items(), key=lambda kv: min(own.index(s) for s in kv[1]))):
        tk = keys_of[k]
        best = max(set(tk), key=tk.count) if tk else ""
        if best and not any(best in keys_of[o] for o in keys_of if o != k):  # its titles' own words ('Secondary')
            names[k] = " ".join(w if re.fullmatch(r"[A-Z0-9]{1,3}", w) else w.capitalize() for w in best.split())
            continue
        mine = set.intersection(*(lines[s["id"]] for s in g)) if g else set()
        others = set().union(*(lines[s["id"]] for o, og in comps.items() if o != k for s in og))
        cand = [t for t in sorted(mine - others, key=lambda t: (abs(len(t) - 20), t)) if not re.search(r"\d", t)]
        if not cand:  # the words every file name of the group has and no other group's ('…_primary_type_2_…')
            stems = lambda gg: [set(_TITLE_NOISE.sub(" ", re.sub(r"[_\-.]+", " ", Path(x["file"]).stem)).lower().split())
                                for x in gg if whole_plan(x)]
            common = set.intersection(*stems(g)) if stems(g) else set()
            other = set().union(*(w for o, og in with_plans.items() if o != k for w in stems(og)))
            cand = sorted(w for w in common - other if len(w) >= 4 and w.isalpha() and w not in _BLOCK_LABELS)
            cand = [" ".join(cand[:3])] if cand else []
        names[k] = cand[0].title()[:40] if cand else f"Building {i + 1}"
    return with_plans, names


def split_buildings(sheets: list[dict], abs_of: dict | None = None) -> list[str]:
    """In place: s['building'] = the building's name ('Special School') when the upload holds several buildings;
    s['building_source'] says from what. Returns the building names (empty = one building, nothing changed)."""
    if any(s.get("building") for s in sheets):  # VP's reader named the buildings from the titles: keep those
        return sorted({s["building"] for s in sheets if s.get("building")})
    own = [s for s in sheets if not s.get("copy_of")]

    def whole_plan(s: dict) -> bool:  # a life safety / furniture / trade plan redraws a level: it names no building
        return s["kind"] == "floor_plan" and not s["partial"] and not _purpose(s)

    groups: dict[str, list[dict]] = {}
    how = ""
    iso = {s["id"]: m.group(1).upper() for s in own if (m := _ISO_NO.match(str(s.get("number") or "")))}
    if len(set(iso.values())) >= 2:
        for s in own:
            if s["id"] in iso:
                groups.setdefault(iso[s["id"]], []).append(s)
        how = "drawing number"
    else:
        for s in own:
            k = _title_key(s.get("title") or "")
            if k and s.get("medium", "vector") == "vector" and not _purpose(s):  # 'LIFE SAFETY' is no building
                groups.setdefault(k, []).append(s)
        how = "title"
    if how == "title":  # 'special school' and 'special school annex wing'? one building when one name holds the other
        for k in sorted(groups, key=len):
            big = [o for o in groups if o != k and len(o) > len(k) and set(k.split()) <= set(o.split())]
            if big and k in groups:
                for o in big:
                    groups[k].extend(groups.pop(o))
    with_plans = {k: g for k, g in groups.items() if any(whole_plan(s) for s in g)}
    if how == "title":  # a building drawn by its titles has whole plans of 2+ levels (USACE fuel set: 18 titled
        with_plans = {k: g for k, g in with_plans.items()  # structures, one plan each, were 18 'buildings')
                      if len({s["storey"] for s in g if whole_plan(s) and s["storey"] is not None}) >= 2}
    ok = len(with_plans) >= 2
    if ok and how == "title":  # a title word alone splits only when it explains one level drawn twice
        per_storey: dict = {}
        for k, g in with_plans.items():
            for s in g:
                if whole_plan(s) and s["storey"] is not None:
                    per_storey.setdefault(s["storey"], set()).add(k)
        ok = any(len(ks) >= 2 for ks in per_storey.values())
    block_names: dict = {}
    if not ok:  # plain titles, no ISO numbers: the title block's own lines (project name, address)
        groups, block_names = _block_groups(own, abs_of or {}, whole_plan)
        if not groups:
            return []
        with_plans, how = groups, "title block"
    # a readable name per group: its most common title words ('Special School'), else its code ('SEN')
    names: dict[str, str] = dict(block_names)
    for k, g in with_plans.items():
        if k in names:
            continue
        keys = [x for x in (_title_key(s.get("title") or "") for s in g) if x]
        best = max(set(keys), key=keys.count) if keys else ""
        names[k] = (" ".join(w if re.fullmatch(r"[A-Z0-9]{1,3}", w) else w.capitalize() for w in best.split())
                    if best else k)
    if len(set(names.values())) < len(names):
        names = {k: (f"{v} ({k})" if how == "drawing number" else v) for k, v in names.items()}
    # what tells one group from the others: the words of its name the other names do not have ('primary')
    words = {k: set(v.lower().split()) for k, v in names.items()}
    own_words = {k: w - set().union(*(words[o] for o in words if o != k)) for k, w in words.items()}
    for s in sheets:
        s.pop("building_source", None)
    for k, g in groups.items():
        for s in g:
            if k in names:
                s["building"], s["building_source"] = names[k], how
    for s in own:  # a sheet no group claimed (another code, no number, a site plan): its title / file names one?
        if s.get("building"):
            continue
        text = f"{s.get('title') or ''} {Path(s['file']).stem}".lower().replace("_", " ").replace("-", " ")
        hits = [k for k, w in own_words.items() if w and any(re.search(rf"\b{re.escape(x)}\b", text) for x in w)]
        if len(hits) == 1:
            s["building"], s["building_source"] = names[hits[0]], "title"
    by_id = {s["id"]: s for s in sheets}
    for s in sheets:  # a copy belongs to its original's building
        o = by_id.get(s.get("copy_of") or "")
        if o is not None and o.get("building"):
            s["building"], s["building_source"] = o["building"], "sheet it copies"
    return sorted(set(names.values()))


def buildings(sheets: list[dict]) -> list[str]:
    """The project's building names (effective sheets), in the order their first sheet comes; [] = one building."""
    out = list(dict.fromkeys(s["building"] for s in effective_all(sheets) if s.get("building")))
    return out if len(out) >= 2 else []


_ALL_BUILDINGS = re.compile(
    r"\b(?:all|both|every|each)\s+(?:of\s+)?(?:the\s+|these\s+|our\s+)?(?:two\s+|three\s+|\d+\s+)?(?:buildings|schools|"
    r"blocks|houses|sites|projects)\b|\b(?:whole|entire)\s+(?:site|campus|estate|portfolio)\b|\bacross\s+(?:all\s+)?"
    r"(?:the\s+)?(?:buildings|schools|site|campus|projects)\b|\b(?:together|combined|in\s+total)\b.*\b(?:buildings|"
    r"schools)\b|\b(?:buildings|schools)\b.*\b(?:together|combined|in\s+total)\b", re.I)


_NOT_A_NAME = {"whole", "entire", "main", "same", "new", "existing", "other", "first", "second", "this", "that", "your",
               "our", "school", "building"}


def target_building(proj: dict, question: str, current: str = "") -> tuple[str | None, str]:
    """Which building a whole-building question is about in a project of several: (name, why) — the one the question
    names ('the primary school'), else the building of the sheet on screen; (None, why) = every building (the question
    says 'all buildings' / 'both schools', or nothing tells). A one-building project -> (None, '')."""
    names = buildings(proj.get("sheets", []))
    if not names:
        return None, ""
    q = str(question or "")
    if _ALL_BUILDINGS.search(q):
        return None, "the question is about every building: each building's levels are added"
    words = {n: set(_WORD.findall(n.lower())) for n in names}
    distinct = {n: w - set().union(*(words[o] for o in names if o != n)) for n, w in words.items()}
    ql = set(_WORD.findall(q.lower()))
    low = _norm_text(q)
    # a word only that building has (3+ letters: 'primary'), or its whole name ('talo b': a lone 'b' / 'a' is no name)
    named = [n for n in names if {w for w in distinct[n] if len(w) >= 3} & ql or _norm_text(n) in low]
    if len(named) == 1:
        return named[0], f"the question names {named[0]}"
    # 'the primary school' when the upload holds a secondary and a special school: say it, then use the sheet's
    known = set().union(*words.values())
    asked = next((m.group(0)[4:] for m in re.finditer(r"\bthe\s+([a-z][a-z-]{2,})\s+(?:school|building|house|block|dormitory)\b",
                                                  q.lower()) if m.group(1) not in known and m.group(1) not in _NOT_A_NAME), "")
    sheets = effective_all(proj.get("sheets", []))
    cur = next((s for s in sheets if s["id"] == current), None) or next(
        (s for s in sheets if current and s["file"] == current.split("#")[0]), None)
    if cur is not None and cur.get("building"):
        if asked:
            return cur["building"], (f"no building in this upload is '{asked}': answered for {cur['building']}, the "
                                     f"building of the sheet on screen (here: {', '.join(names)})")
        return cur["building"], f"{cur['building']}: the building of the sheet on screen (ask about 'all buildings' to add the others)"
    return None, "several buildings and none named: each building's levels are added (ask on one building's sheet for that one)"


def _guess_name(sheets: list[dict], files: list[str]) -> str:
    """'Secondary School' from 'Secondary School GA Plan Level 00' / '... Level 01' (the words every title shares)."""
    names = list(dict.fromkeys(s["building"] for s in sheets if s.get("building")))
    if len(names) >= 2:  # several buildings: name them all
        return " + ".join(names)[:80]
    def strip(t: str) -> list[str]:
        t = re.sub(r"\b(?:level|floor|storey|story)\s*-?\d{1,3}\b", " ", t, flags=re.I)
        return re.sub(r"\b(ga|plan|plans|level|floor|storey|ground|first|second|third|fourth|fifth|basement|roof)\b", " ",
                      t, flags=re.I).split()
    # copies and OCR-read titles last: 'Special Schoo! Plan Level 00' (a scan) must not cut 'School' from the name
    own = [s for s in sheets if s.get("title") and not s.get("copy_of")]
    plans = [s for s in own if s.get("kind") == "floor_plan"]
    clean = [s for s in plans if s.get("medium", "vector") == "vector"]
    titles = [strip(s["title"]) for s in (clean if len(clean) >= 2 else plans if len(plans) >= 2 else own)]
    if titles:
        common = [w for w in titles[0] if all(w in t for t in titles[1:])]
        if common:
            return " ".join(common)[:60]
    if len(files) == 1:  # one set PDF: its file name ('DAF_Dorm_SD_Drawings_April_2026.pdf' -> 'DAF Dorm SD Drawings…')
        return re.sub(r"[_\-.]+", " ", Path(files[0]).stem).strip()[:60]
    return f"{len(files)} uploaded sheets"


def _order(k) -> float:
    if k == "roof":
        return 1000.0
    if k == "mezzanine":
        return 0.5
    try:
        return float(k)
    except (TypeError, ValueError):
        return 999.0


def storeys(sheets: list[dict]) -> list[dict]:
    """The level table for the switcher: [{storey, level, sheets: [id]}], lowest first; sheets without a level last."""
    order = _order
    rows: dict = {}
    every = _one_storey_plans(effective_all(sheets))[0]  # a one-storey set's unlevelled floor plan: under that level
    # a level exists when a PLAN of it does: 'P-501 PLUMBING DETAILS' / 'FA-602 FIRE ALARM MATRIX' print a level word
    # but are no level of their own (FTC: 'Ground floor' and 'Level 4' from them); the user's own level always counts
    planned = {(s.get("building") or "", s["storey"]) for s in every if s["storey"] is not None and (
        s["kind"] in ("floor_plan", "drawing", "unknown", "rcp", "roof_plan") or s.get("level_source") == "you")}
    for s in every:
        st = s["storey"] if (s.get("building") or "", s["storey"]) in planned else None
        key = (s.get("building") or "", st if st is not None else "?")
        r = rows.setdefault(key, {"storey": st, "level": (s["level"] if st is not None else "") or "No level read",
                                  "building": s["building"], "sheets": []})
        r["sheets"].append(s["id"])
    return sorted(rows.values(), key=lambda r: (not r["building"], r["building"] or "", r["storey"] is None,
                                                order(r["storey"])))  # sheets placed in no building: last


def view(proj: dict) -> dict:
    """What the web shows: sheets with their effective labels, the level table, the review list, open questions."""
    sheets = _one_storey_plans(effective_all(proj.get("sheets", [])))[0]  # as plan() / route() / the level table
    for s in sheets:
        s.pop("_pages", None)
    items = proj.get("items", [])
    return {"id": proj["id"], "name": proj.get("name", ""), "files": proj.get("files", []), "sheets": sheets,
            "levels": storeys(proj.get("sheets", [])), "items": [i for i in items if i.get("status") != "open"],
            "open": [i for i in items if i.get("status") == "open"], "rev": proj.get("rev", 0),
            "updated": proj.get("updated", ""), "corrections": _f7() is not None}


# ------------------------------------------------------------------------------------------------- building scope
_BUILDING_Q = re.compile(r"\b(whole|entire|all|every|total|in total|altogether|combined)\b.*\b(building|school|house|"
                         r"project|floors?|levels?|storeys?|stories|sheets?)\b|\b(building|project)\b", re.I)


_SCOPE_WORDS = [
    (re.compile(r"\b(?:the|this)\s+(?:whole\s+|entire\s+)?(?:building|school|house|project)'s\s+", re.I), "the "),
    (re.compile(r"\s*\b(?:in|of|for|across|on|throughout|within)\s+(?:the\s+|this\s+)?(?:whole|entire|complete)\s+"
                r"(?:building|school|house|home|project|set|facility|dormitory|dorm)\b", re.I), ""),
    (re.compile(r"\s*\b(?:in|on|across|over)\s+(?:all|every|each)\s+(?:of\s+)?(?:the\s+)?(?:floors?|levels?|storeys?|"
                r"stories|sheets?|plans?|drawings?)\b", re.I), ""),
    (re.compile(r"\s*\b(?:in|of|for|across)\s+(?:the|this)\s+(?:building|school|house|project|facility)\b", re.I), ""),
    (re.compile(r"\b(?:does|do)\s+(?:the|this)\s+(?:whole\s+|entire\s+)?(?:building|school|house|project)\s+"
                r"(?:have|contain|include)\b", re.I), "are there"),
    (re.compile(r"\b(?:the|this)\s+(?:whole|entire)\s+(?:building|school|house|project)(?:'s)?\b", re.I), "this plan"),
    (re.compile(r"\s*\b(?:in\s+total|altogether|combined|overall)\b", re.I), ""),
]
_THIS_SHEET = re.compile(r"\b(?:this|the)\s+(?:plan|sheet|drawing|floor|level|storey|page)\b", re.I)


def building_neutral(question: str, names: list[str]) -> str:
    """The question with a building of this project named by its name ('the secondary school', 'Talo B') said as 'the
    building', so the per-sheet question and the floor-area test read it like any whole-building question: 'How many
    doors does the secondary school have?' -> 'How many doors does the building have?' (asked on one sheet verbatim,
    'secondary' matched the 'Secondary Classroom' labels: 21 doors, a rough guess)."""
    q = str(question or "")
    for n in sorted((x for x in names or [] if x and x != OTHER_SHEETS), key=len, reverse=True):
        words = [re.escape(w) for w in re.split(r"\s+", n.strip()) if w]
        if words:
            q = re.sub(r"\b(?:the\s+)?" + r"\s+".join(words) + r"(?:'s)?\b", "the building", q, flags=re.I)
    return q


def sheet_question(question: str) -> str:
    """The building question as it is asked on ONE sheet: 'How many doors are there in the whole building?' ->
    'How many doors are there on this plan?' (a sheet reader abstains on 'whole building': 'the sheet prints no
    total'). The question is unchanged when it names no building scope."""
    q = str(question or "").strip()
    out = q
    for rx, rep in _SCOPE_WORDS:
        out = rx.sub(rep, out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"\s+([?.!,])", r"\1", out)
    if out == q:
        return q
    if not _THIS_SHEET.search(out):
        end = "?" if q.endswith("?") or not q.endswith((".", "!")) else q[-1]
        out = re.sub(r"[?.!]*$", "", out).rstrip() + " on this plan" + end
    return out


# ------------------------------------------------------------------ one named room (M1 21:36: never summed over levels)
_TXT: dict = {}
_GENERIC = set("""a an the of in on at for to from with and or by is are was be does do did has have there their this that
these those it its what which how many much big large small size total overall whole entire all every each any area areas
floor floors level levels storey storeys story stories plan plans sheet sheets drawing drawings page building buildings
school house home project facility site room rooms space spaces door doors window windows wall walls skirting skirtings
baseboard baseboards perimeter length width depth height volume gross net internal external square metres meters metre
meter feet foot ft m m2 sq sqm m² ft² number count counted quantity please tell me give find you can we our measured
around inside within about long wide high deep ground first second third fourth upper lower basement roof mezzanine
need order ordered get lay laid fit fitted cover covered paint painted finish finished flooring carpet tiles tile""".split())
_WORD = re.compile(r"[a-z0-9]+")


def _norm_text(s: str) -> str:
    """Lower case, any run of non-letters/digits -> one space ('MULTI-PURPOSE\\nHALL' = 'multi purpose hall')."""
    return " " + " ".join(_WORD.findall(str(s or "").lower())) + " "


def _sheet_text(sh: dict) -> str | None:
    """A sheet's own printed words, normalised: a vector PDF page's text ($0, ~50 ms, cached by size + mtime), an
    image's text layer only when it is already in memory (never a cold OCR / VLM read). None = not known."""
    try:
        ap = resolve(sh["file"])
        st = os.stat(ap)
    except (OSError, ValueError, KeyError):
        return None
    key = (ap, int(sh.get("page") or 0), st.st_size, int(st.st_mtime))
    if key in _TXT:
        return _TXT[key]
    txt = None
    if ap.lower().endswith(".pdf"):
        try:
            import fitz

            with fitz.open(ap) as doc:
                t = doc[int(sh.get("page") or 0)].get_text()
            txt = _norm_text(t) if len(t.strip()) >= 20 else None  # a scanned page has no words of its own
        except Exception:  # noqa: BLE001
            txt = None
    else:
        try:
            from stoa.solvers.raster import textlayer as T

            pre = f"{ap}{T.file_tag(ap)}|"
            lay = next((v for k, v in list(T._mem.items()) if str(k).startswith(pre) and hasattr(v, "text")), None)
            txt = _norm_text(lay.text()) if lay is not None and lay.items else None
        except Exception:  # noqa: BLE001
            txt = None
    if txt is not None:
        _TXT[key] = txt
    return txt


_PLURAL_Q = re.compile(r"\b(?:all|every|each|total|totals|combined|altogether|overall|sum|per)\b", re.I)


def named_room(question: str, sheets: list[dict]) -> dict | None:
    """The ONE room a question names ('What is the floor area of the Multi-Purpose Hall?', 'How many doors does the
    hall have?') -> {name, on: [sheet ids printing it], unknown: [sheet ids whose words are not known]}; None for a
    question about a kind of room or the whole building ('How many classrooms…', 'total area of all classrooms').
    The name = the longest run of the question's own words (1-4, no generic words at its ends) printed on a sheet."""
    q = str(question or "")
    if _PLURAL_Q.search(q) or is_floor_area_question(q):
        return None
    words = _WORD.findall(q.lower())
    m = re.match(r"\s*how\s+many\s+([a-z]+(?:\s+[a-z]+)?)", q.lower())
    counted = set(_WORD.findall(m.group(1))) if m else set()  # 'How many classrooms' = a kind: summed as usual
    texts = {s["id"]: _sheet_text(s) for s in sheets}
    known = {k: t for k, t in texts.items() if t}
    if not known:
        return None
    # the sheets' own titles and building names are not rooms: 'does the special / primary school have' (T3 c4)
    titled = set(_WORD.findall(" ".join(f"{s.get('title') or ''} {s.get('building') or ''}" for s in sheets).lower()))
    best = None
    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            g = words[i:i + n]
            if g[0] in _GENERIC or g[-1] in _GENERIC or any(w in counted for w in g):
                continue
            if sum(len(w) for w in g if w not in _GENERIC) < 4 or all(w.isdigit() for w in g):
                continue
            if all(w in titled or w in _GENERIC for w in g):
                continue
            phrase = " " + " ".join(g) + " "
            on = [k for k, t in known.items() if phrase in t]
            if on:
                m2 = re.search(r"[^A-Za-z0-9]+".join(map(re.escape, g)), q, re.I)  # the question's own spelling
                best = {"name": m2.group(0) if m2 else " ".join(g), "on": on, "unknown": [k for k in texts if k not in known]}
                break
        if best:
            break
    return best


_CEILING_Q = re.compile(r"\b(?:ceilings?|lights?|lighting|luminaires?|light\s+fixtures?|diffusers?|grilles?|"
                        r"sprinkler\s+heads?|soffits?|downlights?)\b", re.I)


def plan(proj: dict, question: str, current: str = "") -> dict:
    """The sheets a building-level question needs, and why the others are left out. In a project of several
    buildings: the building the question names, else the one of the sheet on screen (`current`), else all."""
    every = effective_all(proj.get("sheets", []))
    chosen, why_not, reason, conf = [], {}, "", 0.6
    target, b_why = target_building(proj, question, current)
    names = buildings(proj.get("sheets", []))
    sheets = every
    if target:
        sheets = [s for s in every if s["building"] == target]
        for s in every:
            if s["building"] != target:
                why_not[s["id"]] = (f"another building ({s['building']}): ask about 'all buildings' to add it"
                                    if s["building"] else "not placed in a building (set its building under Change)")
    elif names:
        sheets = [s for s in every if s["building"]]
        for s in every:
            if not s["building"]:
                why_not[s["id"]] = "not placed in a building (set its building under Change)"
    try:
        from stoa.sheet.sets import Sheet, pick_sheets

        objs = [Sheet(file=s["id"], page=0, number=s["number"] or None, title=s["title"] or s["role"],
                      kind=s["kind"] if s["kind"] not in ("unknown",) else "drawing", storey=s["storey"],
                      storey_name=s["level"] or None, building=s["building"] or None, partial=s["partial"],
                      ratio=s.get("ratio"), page_ratio=s.get("page_ratio"))  # VP: a 1:96 enlargement among 1:480 plans
                for s in sheets]
        picked, p = pick_sheets(question, objs)
        ids = {o.file for o in picked}
        reason, conf = p.reason, float(p.confidence)
    except Exception as e:  # noqa: BLE001 - VP's picker absent / failed: every floor plan
        ids = {s["id"] for s in sheets if s["kind"] in ("floor_plan", "drawing", "unknown")}
        reason = f"every floor plan (sheet picker unavailable: {type(e).__name__})"
    by_id = {s["id"]: s for s in sheets}
    for s in sheets:
        orig = by_id.get(s.get("copy_of") or "")
        if s.get("include") is False:
            why_not[s["id"]] = "you left this sheet out"
        elif s.get("include") is True:
            chosen.append(s)
        elif orig is not None and orig.get("include") is not False:
            why_not[s["id"]] = (f"a copy of {orig['label']} ({s.get('copy_how') or 'the same drawing'}): counted once, "
                                f"from that sheet")
        elif s["kind"] in NOT_SUMMED and not (s["kind"] == "rcp" and s["id"] in ids and _CEILING_Q.search(question)):
            why_not[s["id"]] = f"a {s['role'].lower()}: its quantities are not added to the building's"
        elif s["id"] in ids:
            chosen.append(s)
        elif s["partial"]:
            why_not[s["id"]] = f"an enlarged / partial plan of {s['level'] or 'a part of the building'} (its level has a whole plan)"
        else:
            why_not[s["id"]] = "not the kind of sheet this question is about"
    # a partial / enlarged plan of a level that also has a whole plan: counted once (the whole one)
    chosen, one_storey = _one_storey_plans(chosen)
    if one_storey:
        reason = f"{reason}; {one_storey}" if reason else one_storey
    whole = {(s["building"], s["storey"]) for s in chosen if not s["partial"] and s["storey"] is not None}
    keep = []
    levelled = any(s["storey"] is not None for s in chosen)
    for s in chosen:
        if s.get("include") is True:
            keep.append(s)
        elif s["partial"] and ((s["building"], s["storey"]) in whole or (s["storey"] is None and levelled)):
            why_not[s["id"]] = f"an enlarged / partial plan of {s['level'] or 'a part of the building'}" + (
                " that has a whole plan" if s["storey"] is not None else "")
        elif s["storey"] is None and levelled and s.get("kind_source") != "not read":
            # the other plans print their level and this one does not: an enlarged / typical / unit plan, most likely
            why_not[s["id"]] = ("no level printed on it while the other plans name theirs (an enlarged or typical "
                                "plan?): name its level under Change to count it")
        else:
            keep.append(s)
    keep = _one_plan_per_level(question, keep, why_not)
    # ONE named room ('the Multi-Purpose Hall'): only the level(s) that print its name are asked (never summed)
    room = named_room(question, keep) if len(keep) >= 2 else None
    if room:
        ok = set(room["on"]) | set(room["unknown"])
        for s in keep:
            if s["id"] not in ok:
                why_not[s["id"]] = f"'{room['name']}' is not printed on this sheet"
        keep = [s for s in keep if s["id"] in ok]
        reason = f"the question names one room ('{room['name']}'): only the sheets that print it"
    border = {n: i for i, n in enumerate(names)}
    keep.sort(key=lambda s: (border.get(s["building"], 99), s["building"] or "", s["storey"] is None, _order(s["storey"]),
                             s["file"], s["page"]))
    if b_why:
        reason = f"{b_why}; {reason}" if reason else b_why
    qn = building_neutral(question, names)
    return {"sheets": [{"id": s["id"], "file": s["file"], "page": s["page"], "label": s["label"], "level": s["level"],
                        "building": s["building"], "role": s["role"]} for s in keep], "room": room,
            "skipped": [{"id": s["id"], "label": s["label"], "why": why_not[s["id"]]} for s in every if s["id"] in why_not],
            "reason": reason, "confidence": conf, "sheet_question": sheet_question(qn),
            "floor_area": is_floor_area_question(qn), "building": target or "", "buildings": names}


# Plans drawn for one purpose (a US / UK set draws each level several times: FTC A-101 LIFE SAFETY PLAN - FIRST FLOOR,
# A-111 FLOOR PLAN - FIRST FLOOR, A-121 REFLECTED CEILING PLAN - FIRST FLOOR, furniture plans …): (name, title words,
# the question words that ask for exactly that plan)
_PURPOSES = (
    ("life safety plan", r"life[\s-]*safety|egress|fire\s+(?:strategy|safety|compartment)|code\s+(?:plan|analysis)|"
                         r"paloturvallisuus|brandskydd|brandschutz|flucht", r"\b(?:fire|extinguish\w*|exit|egress|"
                         r"escape|travel\s+distance|occupan\w*|smoke|sprinkler|alarm|life\s+safety|rated)\b"),
    ("furniture plan", r"furniture|furnishing|ff\s*&\s*e|kaluste|möbler|möblierung|equipment\s+plan",
     r"\b(?:furniture|desks?|chairs?|tables?|beds?|seats?|seating|lockers?|bench\w*|equipment|workstations?)\b"),
    ("ceiling plan", r"reflected\s+ceiling|\brcp\b|ceiling\s+plan|alakatto|innertak|deckenspiegel",
     r"\b(?:ceilings?|lights?|lighting|luminaires?|fixtures?|diffusers?|grilles?|soffits?)\b"),
    ("finish plan", r"finish(?:es)?\s+plan|floor\s+finish|flooring\s+plan|pattern\s+plan",
     r"\b(?:finish\w*|flooring|carpet|tiles?|vinyl|paint\w*)\b"),
    ("demolition plan", r"demolition|demo\s+plan|purku|rivning|abbruch", r"\b(?:demoli\w*|removed?|existing)\b"),
    ("services plan", r"electrical|lighting\s+plan|power\s+plan|plumbing|mechanical|hvac|telecom|data\s+plan|"
                      r"fire\s+protection|sprinkler\s+plan|security\s+plan|signage", r"\b(?:outlets?|sockets?|"
                      r"receptacles?|switch\w*|panels?|ducts?|pipes?|piping|plumbing|fixtures?|sinks?|wc|signs?|"
                      r"cameras?|electrical|mechanical)\b"),
    ("structural plan", r"framing|foundation|structural|structure\s+plan|slab\s+plan", r"\b(?:beams?|columns?|joists?|"
                        r"footings?|piles?|slab|framing|rebar|girders?)\b"),
)


# US National CAD Standard discipline letters of a sheet number ('FA-101', 'M-102', 'S-104'): a plan of that trade
_DISCIPLINE = {"S": "structural plan", "AD": "demolition plan", "D": "demolition plan", "Q": "furniture plan",
               **{d: "services plan" for d in ("M", "P", "E", "T", "FA", "FP", "FS", "EP", "EL", "PL", "H", "MP",
                                                "MH", "LV", "TC", "TN", "Y")}}
# a sheet titled as one of these is not a plan to count from, even when its title names a level
_NOT_A_PLAN_TITLE = re.compile(r"\b(?:schedules?|matrix|details?|sections?|elevations?|legends?|notes|diagrams?|"
                               r"risers?|isometrics?|axonometric|perspectives?|renderings?)\b", re.I)
_PLAN_WORD = re.compile(r"\bplans?\b|plaan|pohja|piirustus|grundriss|våningsplan|layout|\bga\b", re.I)


def _one_storey_plans(chosen: list[dict]) -> tuple[list[dict], str]:
    """A one-storey building whose ONLY levelled plans are purpose plans (FuT: FA-101 fire alarm, FP-101 fire
    suppression, T-101 telecom, S-400 slab plan all print 'first floor'; A-111 'FLOOR PLAN' prints no level): its
    unlevelled whole plans are plans of that storey too, so the general floor plan answers (it was left out as 'no level
    printed while the others name theirs', and a door count was asked on the slab plan)."""
    lv = [s for s in chosen if s["storey"] is not None and s.get("include") is not False
          and s["kind"] in ("floor_plan", "drawing") and not s.get("copy_of")]
    keys = {(s["building"], s["storey"]) for s in lv}
    if len(keys) != 1 or any(not _purpose(s) for s in lv):
        return chosen, ""
    (b, k), = keys
    if k in ("roof", "attic") or str(k).startswith("-"):
        return chosen, ""
    lvl = lv[0]["level"]
    out, took = [], []
    for s in chosen:
        if s["storey"] is None and not s["partial"] and s["kind"] == "floor_plan" and s.get("building", "") == b \
                and s.get("kind_source") != "not read":
            s = dict(s, storey=k, level=lvl, level_inferred=True)
            took.append(s)
        out.append(s)
    if not any(not _purpose(s) for s in took):
        return chosen, ""  # no general plan among them: nothing better to ask than the purpose plans
    names = ", ".join(str(s.get("number") or s["label"]) for s in took[:4])
    return out, (f"{names}: no level printed, taken as {lvl or 'the same level'} (the only level the set's other plans "
                 f"name: a one-storey building)")


def _purpose(s: dict) -> str:
    """The purpose a plan was drawn for, from its own title ('LIFE SAFETY PLAN - FIRST FLOOR' -> 'life safety plan')
    or its sheet number's discipline ('FA-101' -> 'services plan'); '' for a general floor plan. A combined title
    ('FLOOR PLAN & RCP') is general."""
    t = f"{s.get('title') or ''} {s.get('plan_line') or ''}"
    if re.search(r"\bfloor\s+plan\s*(?:&|and|\+)\s*(?:rcp|reflected|ceiling)", t, re.I):
        return ""
    for name, words, _ in _PURPOSES:
        if re.search(words, t, re.I):
            return name
    m = re.fullmatch(r"([A-Z]{1,2})-?\d{1,4}[A-Z]?(?:\.\d+)?", str(s.get("number") or "").strip().upper())
    return _DISCIPLINE.get(m.group(1), "") if m else ""


def _one_plan_per_level(question: str, keep: list[dict], why_not: dict) -> list[dict]:
    """A level drawn as several plans (floor plan + life safety + furniture + ceiling plan): ask ONE of them, so a total
    never adds the same doors twice. The general floor plan answers, unless the question asks what a purpose plan is
    drawn for ('fire extinguishers' -> the life safety plan, 'light fixtures' -> the ceiling plan)."""
    by_level: dict = {}
    for s in keep:
        if s["storey"] is not None and s.get("include") is not True:
            by_level.setdefault((s["building"], s["storey"]), []).append(s)
    drop: set = set()
    for rs in by_level.values():
        if len(rs) < 2:
            continue
        kinds = {s["id"]: _purpose(s) for s in rs}
        gen = [s for s in rs if not kinds[s["id"]]]
        if len(gen) >= 2:  # two 'general' sheets of one level: the one titled as a plan, drawn at the smallest scale
            titled = [s for s in gen if _PLAN_WORD.search(f"{s.get('title') or ''} {s.get('plan_line') or ''}")]
            pool = titled or gen
            rat = [float(s.get("page_ratio") or s.get("ratio") or 0) for s in pool]
            big = max(rat) if rat else 0
            whole = [s for s, r in zip(pool, rat) if not big or r >= big / 1.5]
            if len(whole) < len(gen):
                for s in gen:
                    if s not in whole:
                        kinds[s["id"]] = "x"  # dropped below with its reason
                        drop.add(s["id"])
                        why_not[s["id"]] = (f"{'not titled as a plan' if s not in titled and titled else 'drawn at a larger scale'}"
                                            f" while {whole[0]['label']} is the plan of {whole[0]['level'] or 'this level'}"
                                            f" (one plan per level, never added twice)")
        live = {k: v for k, v in kinds.items() if v != "x"}
        if len(set(live.values())) < 2:
            continue  # all general (or all one purpose): the combine step says when they disagree
        asked = [name for name, _, cue in _PURPOSES if re.search(cue, question, re.I) and name in kinds.values()]
        want = asked[0] if asked else ("" if "" in kinds.values() else None)
        if want is None:
            continue  # only purpose plans of different kinds and the question names none: keep them all
        best = next(s for s in rs if kinds[s["id"]] == want)
        for s in rs:
            if s is not best and kinds[s["id"]] not in (want, "x"):
                drop.add(s["id"])
                why_not[s["id"]] = (f"a {kinds[s['id']] or 'general floor plan'} of {s['level'] or 'this level'}: "
                                    f"{best['label']} answers for that level (one plan per level, never added twice)")
    return [s for s in keep if s["id"] not in drop]


def level_in_question(question: str) -> str | None:
    """The storey key a question names ('on the first floor' -> '1', 'Level 02' -> '2', 'ground floor' -> '0',
    'basement' -> '-1', 'roof' -> 'roof'); None when it names none or several."""
    q = re.sub(r"\b(?:this|that|each|every|per|all|whole|entire)\s+(?:floors?|levels?|storeys?|stories)\b", " ", str(question or ""),
               flags=re.I)
    k, _ = level_from_name(re.sub(r"\s+", "_", q.strip()) + ".x")
    return k


_ORDW = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
         "tenth": 10}
_Q_NUM = re.compile(r"\b(?:(\d{1,2})(?:st|nd|rd|th)|(" + "|".join(_ORDW) + r"))\s+(?:floor|level|storey|story)\b|"
                    r"\b(?:floor|level|storey|story)\s+(\d{1,2})\b", re.I)
_Q_LEVEL = re.compile(r"\b(?:on|in|at|of|for|from)\s+(?:the\s+)?(?:" + _TEXT_LEVEL.pattern +
                      r"|\b(?:basement|roof|mezzanine|attic)\b)(?:\s+plan)?"
                      # the phrase ends the clause: 'the first floor landing' is a room, not where to look
                      r"(?=\s*[?.,;!]|\s*$|\s+(?:of|in|for|with|and|or|only)\b)", re.I)


def level_free(question: str) -> str:
    """'How many doors are on the 3rd floor?' -> 'How many doors are on this plan?': asked on that level's own sheet,
    whose readers do not know which floor it is ('on the 3rd floor' made the DfE / Esplanades readers fall back)."""
    q = _Q_LEVEL.sub("on this plan", str(question or ""), count=1)
    # '… on this plan of the primary school?': the building is settled by the routing; its words would only confuse
    return re.sub(r"\bon this plan\s+(?:of|in|at)\s+(?:the\s+)?[\w' -]{2,40}?(?=\s*[?.,;!]|\s*$)", "on this plan", q, count=1)


def _by_printed_number(sheets: list[dict], question: str, k: str | None) -> tuple[str | None, str]:
    """The visitor's floor number read against the levels' PRINTED names: sheets that number floors from 1 ('1.korrus' =
    the entrance floor = storey 0, Finnish '1. kerros' too) mean '3rd floor' = '3 korrus' (storey 2), not storey 3."""
    m = _Q_NUM.search(str(question or ""))
    if not m:
        return k, ""
    n = int(m.group(1) or m.group(3) or 0) or _ORDW.get((m.group(2) or "").lower(), 0)
    plans = [s for s in sheets if s.get("storey") is not None and s["kind"] in ("floor_plan", "drawing")]
    nums = {s["storey"]: [int(x) for x in re.findall(r"\d+", s.get("level") or "")] for s in plans}
    nums = {st: v for st, v in nums.items() if v}  # 'Kelder' / 'Roof' have no number: the others still count from 1
    if not n or len(nums) < 2 or any(len(v) != 1 for v in nums.values()):
        return k, ""
    hit = {st for st, v in nums.items() if v[0] == n}
    if len(hit) != 1 or next(iter(hit)) == k:
        return k, ""
    st = next(iter(hit))
    name = next(s["level"] for s in plans if s["storey"] == st)
    return st, f"'{m.group(0)}' read as {name}, the sheet that prints that number (these sheets number their floors from 1)"


def route(proj: dict, question: str, current: str = "") -> dict | None:
    """A question that names a level ('How many doors are on the 3rd floor?') -> that level's whole floor plan with the
    question to ask there ('… on this plan?') and why; here=True when that plan is the sheet on screen. None when the
    question names no level, or no single plan of it (ask the sheet on screen as it is)."""
    k = level_in_question(question)
    if k is None and not _Q_NUM.search(str(question or "")):
        return _here_with_tags(proj, question, current)
    sheets = effective_all(proj.get("sheets", []))
    k, why = _by_printed_number(sheets, question, k)
    if k is None:
        return _here_with_tags(proj, question, current)
    cur = next((s for s in sheets if s["id"] == current), None) or next(
        (s for s in sheets if s["file"] == current.split("#")[0]), None)
    target = None
    if buildings(proj.get("sheets", [])):  # several buildings: that level of the building the question names / is open
        target, _ = target_building(proj, question, current)
        if target is None and cur is not None and cur.get("building"):
            target = cur["building"]
    if cur is not None and cur["storey"] == k and (target is None or cur.get("building") == target):
        return {"id": cur["id"], "file": cur["file"], "page": cur["page"], "label": cur["label"], "level": cur["level"],
                "here": True, "question": level_free(question), "why": why, "tags": schedule_tags(proj, cur, question)}
    kinds = ("floor_plan", "drawing", "rcp") if _CEILING_Q.search(str(question or "")) else ("floor_plan", "drawing")
    pool = [s for s in sheets if s["kind"] in kinds and s.get("include") is not False
            and (not s.get("copy_of") or s.get("include") is True)]
    pool, _ = _one_storey_plans(pool)  # a one-storey set whose floor plan prints no level (FuT A-111)
    cands = [s for s in pool if s["storey"] == k]
    if target is not None:
        cands = [s for s in cands if s["building"] == target]
    whole = [s for s in cands if not s["partial"]] or cands
    if len(whole) > 1:  # the level drawn several times (floor / life safety / furniture / trade plans): its floor plan
        whole = _one_plan_per_level(question, whole, {})
    if len(whole) != 1:
        return None  # none, or two plans of that level: the sheet on screen answers (and may ask which)
    s = whole[0]
    return {"id": s["id"], "file": s["file"], "page": s["page"], "label": s["label"], "level": s["level"],
            "here": False, "question": level_free(question), "why": why, "tags": schedule_tags(proj, s, question)}


def _here_with_tags(proj: dict, question: str, current: str) -> dict | None:
    """A door count asked on the sheet on screen (no level named) of a set with a door schedule: that sheet, here, with
    its schedule tags (FuT A-111 'How many doors are on this plan?': 21 tags, not the 1 symbol read); else None."""
    if not current or not (_DOOR_COUNT_Q.search(question or "") and not _DOOR_KIND_Q.search(question or "")):
        return None
    sheets = _one_storey_plans(effective_all(proj.get("sheets", [])))[0]
    cur = next((s for s in sheets if s["id"] == current), None)
    if cur is None:
        same = [s for s in sheets if s["file"] == current.split("#")[0]]
        cur = same[0] if len(same) == 1 else None
    tags = schedule_tags(proj, cur, question) if cur else None
    if not tags:
        return None
    return {"id": cur["id"], "file": cur["file"], "page": cur["page"], "label": cur["label"], "level": cur["level"],
            "here": True, "question": question, "why": "", "tags": tags}


def schedule_tags(proj: dict, sheet: dict, question: str) -> dict | None:
    """A plain door count asked on one plan of a set with a door schedule: the schedule's doors tagged on that plan
    {n, total, schedule, evidence (each tag's box), note}; the web answers the level with them (the door symbols read
    there are the cross-check: FuT A-111 1 symbol vs 21 tags), so a level and the building total agree. None when the
    set has no schedule or the plan prints < 3 of its marks."""
    if not (_DOOR_COUNT_Q.search(question or "") and not _DOOR_KIND_Q.search(question or "")):
        return None
    if _purpose(sheet):  # a life safety / furniture / ceiling plan tags only some doors (FTC A-101: 14 of 43)
        return None
    try:
        ds = door_schedule(proj, sheet.get("building") or "")
        if not ds or ds["sheet"]["id"] == sheet["id"]:
            return None
        _, boxes = _page_marks(resolve(sheet["file"]), int(sheet["page"]))
    except Exception:  # noqa: BLE001
        return None
    here = sorted(m for m in boxes if m in ds["marks"])
    if len(here) < 3:
        return None
    lab = ds["sheet"]["label"]
    return {"n": len(here), "total": len(ds["marks"]), "schedule": lab,
            "note": f"the plan's own door tags: {len(here)} doors of the door schedule on {lab} "
                    f"({len(ds['marks'])} doors in all) are tagged on this plan",
            "evidence": [{"file": sheet["file"], "page": sheet["page"], "bbox": boxes[m], "bbox_space": "pdf_pt",
                          "note": f"door {m} (in the door schedule on {lab})"} for m in here]}


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


_UNIT_TO_BASE = {"count": ("count", 1.0), "": ("count", 1.0), None: ("count", 1.0),
                 "m2": ("m2", 1.0), "m²": ("m2", 1.0), "sqm": ("m2", 1.0), "ft2": ("m2", 0.09290304),
                 "sf": ("m2", 0.09290304), "sq ft": ("m2", 0.09290304), "ft²": ("m2", 0.09290304),
                 "m": ("m", 1.0), "mm": ("m", 0.001), "cm": ("m", 0.01), "ft": ("m", 0.3048), "in": ("m", 0.0254),
                 "m3": ("m3", 1.0), "m³": ("m3", 1.0)}


def _unit(u) -> tuple[str, float] | None:
    k = str(u).strip().lower() if u is not None else None
    return _UNIT_TO_BASE.get(k)


# things drawn on EVERY level they pass through: a building count of them is NOT the sum over the levels
_VERTICAL = re.compile(r"\b(?:stair\w*|staircases?|lifts?|elevators?|escalators?|shafts?|risers?|chimney\w*|flues?|"
                       r"columns?|pillars?|grid\s*lines?|ducts?|ramps?|atri(?:um|a)|voids?|treppe\w*|porras\w*|hissi\w*)\b", re.I)


def combine(proj: dict, question: str, parts: list[dict], current: str = "") -> dict:
    """Per-sheet answers -> a building answer (Answer-shaped dict) + the per-sheet table. No total when the sheets
    cannot be added safely; the reason is said instead (value None, 'per_sheet' still filled)."""
    answer = _combine(proj, question, parts, current)
    # the card reads where an answer applies from its intent (web customer.ts wherePhrase / implausible / recognition):
    # a total over 2+ levels is 'in the whole building', never 'on this drawing' (the sheet on screen), and it may
    # exceed one sheet's size; the sheets' own intent (task, target) is kept, their storey filter is not
    got = [(p.get("answer") or {}).get("intent") for p in parts]
    intent = dict(next((i for i in got if isinstance(i, dict)), None) or {})
    levels = {(r.get("building"), r.get("storey"), r.get("label") if r.get("storey") is None else "")
              for r in answer.get("per_sheet") or [] if _num(r.get("value")) is not None}
    if answer.get("value") is not None and len(levels) >= 2:  # summed levels, or the most on one level (lifts)
        intent["filters"] = {k: v for k, v in dict(intent.get("filters") or {}).items() if k not in ("storey", "level")}
        intent["scope"] = "model"
        if str(intent.get("target") or "").lower() in ("room", "space", "unknown", ""):
            intent.pop("target", None)  # 'How many classrooms …': the card then says the question's own noun, not 'rooms'
    if intent:
        answer["intent"] = intent
    nb = len(answer.get("per_building") or [])
    if answer.get("value") is not None and nb >= 2:  # 'both schools' added: not 'in the whole building' (W5 wherePhrase)
        answer["where"] = "in both buildings" if nb == 2 else f"in all {nb} buildings"
    incs = [(p.get("answer") or {}).get("includes") for p in parts]
    if incs and all(i and i == incs[0] for i in incs):  # every level measured the same way: say it once
        answer["includes"] = incs[0]
        answer["excludes"] = (parts[0].get("answer") or {}).get("excludes") or []
        conv = next((str(x) for x in (parts[0].get("answer") or {}).get("trace") or [] if str(x).startswith("convention:")), "")
        if conv:
            answer["trace"] = [conv, *answer.get("trace", [])]
    if is_floor_area_question(building_neutral(question, buildings(proj.get("sheets", [])))):
        # the set prints its own building total (FTC life safety plans): that one
        answer = _with_printed_total(answer, proj, question)
        answer = _upper_floor_check(answer)
    elif _DOOR_COUNT_Q.search(question or "") and not _DOOR_KIND_Q.search(question or ""):
        answer = _with_door_schedule(answer, proj, question)
    return answer


UPPER_FLOOR_RATIO = 1.25  # an upper level this much larger than every level below it is checked, not trusted


def _upper_floor_check(answer: dict) -> dict:
    """A summed floor area whose upper level measures much more than every level below it (DfE secondary L02 4,116.9 m²
    over L00 3,918.7 / L01 2,638.4 on an old footprint reader: its outline took the roofs over the halls below) is rare
    in a real building (an overhang adds a few %): the total stays, its confidence drops to a best estimate and the
    card / the level's row say which level to check. Basements are not compared (a car park may outgrow the ground)."""
    if answer.get("value") is None or answer.get("solver") != "project/sheets":
        return answer

    def storey(r: dict) -> float | None:
        try:
            return float(r.get("storey"))
        except (TypeError, ValueError):
            return None

    rows = []
    for r in answer.get("per_sheet") or []:
        v, st, u = _num(r.get("value")), storey(r), _unit(r.get("unit"))
        if r.get("counted") and v and st is not None and st >= 0 and u:
            rows.append((r, st, v * u[1]))
    odd = []
    for r, st, v in rows:
        below = [(x, w) for x, s2, w in rows if s2 < st and x.get("building", "") == r.get("building", "")]
        if below:
            big, w = max(below, key=lambda t: t[1])
            if v > UPPER_FLOOR_RATIO * w:
                odd.append((r, big, round((v / w - 1) * 100)))
    if not odd:
        return answer
    answer = dict(answer)
    lines = []
    for r, big, pct in odd:
        name, under = r.get("level") or r.get("label"), big.get("level") or big.get("label")
        r["note"] = f"{pct} % larger than {under} below it: check its outline"
        unit = {"m2": "m²", "ft2": "ft²", "sf": "SF"}.get(str(r.get("unit") or ""), str(r.get("unit") or ""))
        lines.append(f"check: {name} measures {_fmt(_num(r['value']))} {unit}, {pct} % more than "
                     f"{under} below it ({_fmt(_num(big['value']))}); an upper floor is rarely that much larger than the "
                     f"floors below (a roof or deck of the floor below may be inside its outline)".replace(" ,", ","))
    answer["confidence"] = round(min(float(answer.get("confidence") or 0), 0.35), 3)
    answer["assumptions"] = [*(answer.get("assumptions") or []), *lines]
    answer["missing"] = [*(answer.get("missing") or []),
                         *[f"a check of {r.get('level') or r.get('label')}'s outline" for r, _, _ in odd]]
    answer["trace"] = [*(answer.get("trace") or []), f"project: upper-floor check: {len(odd)} level(s) > "
                                                     f"{UPPER_FLOOR_RATIO:.2f} x the largest level below -> confidence <= 0.35"]
    return answer


# ------------------------------------------------------------------ the set's own door schedule (building door counts)
# A US / UK set lists every door in a DOOR SCHEDULE table (FTC A-610: 66 marks '100.A' … '224.A'; MEDIUMFC A-602 93
# '1.00A' … '19.01B'; SFO A-303 117) and prints each door's mark next to it on the floor plans (FTC A-111 43 of them,
# A-112 22). The schedule is the set's own list, every row checkable on a plan: it answers a whole-building door count;
# the door symbols counted on the plans stay as the cross-check.
_DOOR_COUNT_Q = re.compile(r"\b(?:how\s+many|number\s+of|count|total|amount\s+of)\b[^?]*\bdoors?\b|"
                           r"\bdoors?\b[^?]*\b(?:count|in\s+total|altogether)\b", re.I)
# a KIND of door (or a door's size / type) is a schedule column question, not 'how many doors'
_DOOR_KIND_Q = re.compile(r"\b(?:fire|double|pairs?|rated|external|exterior|internal|interior|entrance|front|back|"
                          r"sliding|glass|glazed|steel|wood\w*|metal|aluminium|aluminum|automatic|overhead|roll\w*|"
                          r"garage|types?|leaf|leaves|wide|width|height|swing\w*|hardware|frames?|hollow|solid|panel)\b",
                          re.I)
# a door mark: '100.A', '19.01B', '1.00A', '101A', 'D101', 'D-12' (always a letter: a bare number is a room number)
_DOOR_MARK = re.compile(r"^(?:\d{1,4}\.\d{0,3}[A-Z]{1,2}|\d{1,4}[A-Z]{1,2}|D-?\d{1,4}[A-Z]?)$")
_SCHED_HEAD = re.compile(r"^\s*door\s+schedule\s*$", re.I)
_DS_CACHE: dict = {}


def _mark_shape(m: str) -> str:
    return re.sub(r"[A-Z]+", "A", re.sub(r"\d+", "9", m))


def _page_marks(abs_path: str, page: int) -> tuple[bool, dict]:
    """(this page heads a DOOR SCHEDULE table, {mark: first box}) for the dominant door-mark shape on the page."""
    key = (abs_path, int(page), os.path.getmtime(abs_path))
    if key not in _DS_CACHE:
        import fitz  # noqa: PLC0415

        with fitz.open(abs_path) as d:
            pg = d[int(page)]
            head = any(_SCHED_HEAD.match(ln) for ln in pg.get_text().splitlines())
            boxes: dict = {}
            for w in pg.get_text("words"):
                t = str(w[4]).strip().rstrip(",;")
                if _DOOR_MARK.match(t) and t not in boxes:
                    boxes[t] = [round(float(x), 1) for x in w[:4]]
        _DS_CACHE[key] = (head, boxes)
    return _DS_CACHE[key]


def door_schedule(proj: dict, target: str = "") -> dict | None:
    """The building's door schedule: {sheet, marks {mark: box}} from the vector sheet that heads a DOOR SCHEDULE table
    and lists >= 8 marks of one shape (the shape most of its marks share); None when the set has none."""
    several = len(buildings(proj.get("sheets", []))) >= 2
    best = None
    for s in effective_all(proj.get("sheets", [])):
        if s.get("copy_of") or s.get("medium", "vector") != "vector" or not str(s.get("file", "")).lower().endswith(".pdf"):
            continue
        if several and s.get("building") and s.get("building") != target:
            continue
        try:
            head, boxes = _page_marks(resolve(s["file"]), int(s["page"]))
        except Exception:  # noqa: BLE001 - an unreadable page lists nothing
            continue
        if not head or len(boxes) < 8:
            continue
        shapes: dict = {}
        for m in boxes:
            shapes.setdefault(_mark_shape(m), []).append(m)
        shape, marks = max(shapes.items(), key=lambda kv: len(kv[1]))
        if len(marks) >= 8 and (best is None or len(marks) > len(best["marks"])):
            best = {"sheet": s, "shape": shape, "marks": {m: boxes[m] for m in marks}}
    return best


def _with_door_schedule(answer: dict, proj: dict, question: str) -> dict:
    """A whole-building door count summed from the plans' door symbols -> the set's door schedule when the counted
    plans print its marks (>= 80 % of the schedule found on them, few marks the schedule lacks): the schedule's
    count, each level's tagged doors as its row, every door's tag marked on its plan; the symbol sum is the
    cross-check. Otherwise the symbol sum stays and the schedule's count is said next to it."""
    rows = [r for r in answer.get("per_sheet") or [] if r.get("counted")]
    if not rows or len(answer.get("per_building") or []) >= 2:
        return answer
    if len(buildings(proj.get("sheets", []))) >= 2 and not answer.get("building"):
        return answer
    try:
        ds = door_schedule(proj, answer.get("building") or "")
    except Exception:  # noqa: BLE001
        ds = None
    if not ds:
        return answer
    marks, sh = ds["marks"], ds["sheet"]
    tagged, extra, ev = {}, set(), []
    for r in rows:
        try:
            _, boxes = _page_marks(resolve(r["file"]), int(r["page"]))
        except Exception:  # noqa: BLE001
            boxes = {}
        here = [m for m in boxes if m in marks and m not in tagged]
        extra |= {m for m in boxes if m not in marks and _mark_shape(m) == ds["shape"]}
        for m in here:
            tagged[m] = r
            ev.append({"file": r["file"], "page": r["page"], "bbox": boxes[m], "bbox_space": "pdf_pt",
                       "note": f"{r.get('level') or r.get('label')}: door {m} (in the door schedule on {sh['label']})"})
        r["tags"] = len(here)
    n, traced = len(marks), _num(answer.get("value"))
    cover = len(tagged) / n
    sym = f"{traced:,.0f}" if traced is not None else "none"
    diff = f" ({(traced - n) / n * 100:+.0f} %)" if traced else ""
    out = dict(answer)
    if cover < 0.8 or len(extra) > 0.2 * n:
        out["assumptions"] = [*(answer.get("assumptions") or []),
                              f"cross-check: the door schedule on {sh['label']} lists {n} doors, but only "
                              f"{len(tagged)} of its marks are printed on the plans counted here, so the plans' "
                              f"door symbols ({sym}) answer"][:9]
        out["trace"] = [f"project: door schedule on {sh['id']}: {n} marks, {len(tagged)} on the counted plans, "
                        f"{len(extra)} plan marks not in it: not used", *(answer.get("trace") or [])]
        return out
    each = " + ".join(f"{r.get('level') or r.get('label')} {r['tags']}" for r in rows)
    untagged = sorted(set(marks) - set(tagged))
    per = []
    for r in answer.get("per_sheet") or []:
        r = dict(r)
        if r.get("counted") and "tags" in r:
            r["note"] = f"{r['tags']} door tags of the schedule (door symbols seen: {_fmt(r.get('value'))})"
            r["value"], r["unit"] = r["tags"], "count"
        per.append(r)
    ev += [{"file": sh["file"], "page": sh["page"], "bbox": marks[m], "bbox_space": "pdf_pt",
            "note": f"door schedule: {m}" + ("" if m in tagged else " (not tagged on the counted plans)")}
           for m in sorted(marks)]
    out.update({
        "value": n, "unit": "count", "solver": "project/door_schedule", "per_sheet": per,
        "confidence": 0.85 if cover >= 0.95 else 0.75,
        "evidence": ev[:500],
        "assumptions": [f"listed in the door schedule on {sh['label']}: {n} doors ({min(marks)} … {max(marks)})",
                        f"cross-check: the plans tag {len(tagged)} of them ({each})"
                        + (f"; not tagged on these plans: {', '.join(untagged[:6])}" if untagged else "")
                        + f"; the door symbols counted on the plans add up to {sym}{diff}",
                        *[a for a in answer.get("assumptions") or [] if not a.startswith("one plan per level")]][:8],
        "trace": [f"project: door schedule on {sh['id']}: {n} marks ({ds['shape']}), {len(tagged)} tagged on "
                  f"{len(rows)} counted plan(s), {len(extra)} plan marks not in it; symbol sum {sym}",
                  *(answer.get("trace") or [])],
        "no_total_why": ""})
    return out


_PT_CACHE: dict = {}
_SKIP_FOR_TOTAL = {"site_plan", "detail", "section", "elevation", "axon", "roof_plan", "rcp"}


def _printed_building_total(proj: dict, target: str = "") -> dict | None:
    """A whole building's floor area PRINTED somewhere in its set ('TOTAL GROSS FLOOR AREA = 30,125 GSF' in the code
    table of FTC's life safety plans A-101 / A-102; 'Overall GIFA … Total'): VP's printed_gross over the building's
    vector sheets (~0.04 s a page, cached). Only a figure that every sheet printing one agrees on (within 0.5 %)."""
    try:
        from stoa.sheet.footprint import printed_gross
    except ImportError:
        return None
    found = []
    for s in effective_all(proj.get("sheets", [])):
        if (target and s.get("building") != target) or s.get("copy_of") or s.get("medium", "vector") != "vector" \
                or s["kind"] in _SKIP_FOR_TOTAL:
            continue
        try:
            ab = resolve(s["file"])
            key = (ab, int(s["page"]), os.path.getmtime(ab))
            if key not in _PT_CACHE:
                _PT_CACHE[key] = printed_gross(ab, int(s["page"])) or []
        except Exception:  # noqa: BLE001 - an unreadable page prints nothing
            continue
        for r in _PT_CACHE[key]:
            v, u = _num(r.get("value")), _unit(r.get("unit"))
            if r.get("kind") == "total" and v and v > 0 and u and u[0] == "m2":
                found.append({"m2": v * u[1], "value": v, "unit": r.get("unit"), "text": str(r.get("text") or ""),
                              "bbox": r.get("bbox"), "sheet": s})
    if not found:
        return None
    base = found[0]["m2"]
    if any(abs(f["m2"] - base) > 0.005 * base for f in found):
        return None  # two different printed totals (a phase table, a target vs the design): no single figure
    return found[0] | {"n": len(found), "on": [f["sheet"]["label"] for f in found]}


def _with_printed_total(answer: dict, proj: dict, question: str) -> dict:
    """A whole-building floor-area answer summed from traced levels -> the set's own printed total when there is one
    (its text marked on the sheet); the traced sum stays as the cross-check, its difference said."""
    if _NET_Q.search(question or "") or answer.get("room") or len(answer.get("per_building") or []) >= 2:
        return answer
    pt = _printed_building_total(proj, answer.get("building") or "")
    if not pt:
        return answer
    v = round(pt["m2"], 1)
    sh = pt["sheet"]
    traced = _num(answer.get("value"))
    diff = f"{(traced - v) / v * 100:+.0f} %" if traced and v else ""
    printed = f"{pt['value']:,.0f} {'sq ft' if str(pt['unit']).lower() in ('ft2', 'ft²', 'sf') else 'm²'}"
    note = f"printed on {sh['label']}: '{pt['text'][:80]}'" + (f" (= {v:,.1f} m²)" if 'sq ft' in printed else "")
    cross = (f"cross-check: the traced outlines of the levels add up to {traced:,.1f} m² ({diff} from the printed figure)"
             if traced else "the levels' outlines gave no sum to compare")
    out = dict(answer)
    out.update({
        "value": v, "unit": "m2", "confidence": 0.85 if traced and abs(traced - v) <= 0.1 * v else 0.75,
        "solver": "project/printed_total",
        "evidence": [{"file": sh["file"], "page": sh["page"], "bbox": pt.get("bbox"),
                      "note": f"printed building total: {pt['text'][:120]}"}] + list(answer.get("evidence") or [])[:40],
        "assumptions": [note + ("" if pt["n"] < 2 else f"; the same figure on {pt['n']} sheets"), cross,
                        *[a for a in answer.get("assumptions") or [] if not a.startswith("one plan per level")]][:8],
        "trace": ["convention: gross floor area as the drawing set prints it (its own area table)",
                  f"project: printed building total {printed} on {sh['id']} ({pt['n']} sheet(s)); traced sum {traced}",
                  *[t for t in answer.get("trace") or [] if not str(t).startswith("convention:")]],
        "includes": [], "excludes": [],
        "no_total_why": ""})
    return out


def _combine(proj: dict, question: str, parts: list[dict], current: str = "") -> dict:
    # (a one-storey set's unlevelled floor plan is a plan of that storey, as plan() asked it)
    sheets = {s["id"]: s for s in _one_storey_plans(effective_all(proj.get("sheets", [])))[0]}
    target, b_why = target_building(proj, question, current)
    several = len({(sheets.get(str(p.get("sheet") or "")) or {}).get("building", "") for p in parts}) >= 2
    rows, problems = [], []
    for p in parts:
        sid = p.get("sheet") or f"{p.get('file')}#{int(p.get('page') or 0)}"
        s = sheets.get(sid)
        a = p.get("answer") or {}
        lvl = (s or {}).get("level", "")
        if several and s and s.get("building"):  # 'Primary School · Level 00': which building's level
            lvl = f"{s['building']} · {lvl or 'no level'}"
        rows.append({"sheet": sid, "label": s["label"] if s else sid, "level": lvl,
                     "storey": (s or {}).get("storey"), "building": (s or {}).get("building", ""),
                     "file": (s or {}).get("file", p.get("file")), "page": (s or {}).get("page", p.get("page") or 0),
                     "value": a.get("value"), "unit": a.get("unit"), "confidence": float(a.get("confidence") or 0),
                     "solver": a.get("solver", ""), "evidence": list(a.get("evidence") or [])[:500],
                     "assumptions": [str(x) for x in (a.get("assumptions") or [])[:4]], "counted": False, "note": ""})
        if s is None:
            rows[-1]["note"] = "not a sheet of this project"
    usable = [r for r in rows if r["note"] == ""]
    nums = [(r, _num(r["value"])) for r in usable]
    missing = [r for r, v in nums if v is None]
    for r in missing:
        r["note"] = "no number on this sheet"
        problems.append(f"{r['level'] or r['label']}: no number")
    units = {(_unit(r["unit"]) or ("?" + str(r["unit"]), 1.0))[0] for r, v in nums if v is not None}
    if len(units) > 1:
        problems.append("the sheets answered in different kinds of unit (" + ", ".join(sorted(units)) + ")")
    # one reading per level: the same storey twice = a variant / an overview + its plan
    per_level: dict = {}
    for r, v in nums:
        if v is None:
            continue
        per_level.setdefault((r["building"], r["storey"]), []).append((r, v))
    no_level = [r for r, v in nums if v is not None and r["storey"] is None]
    for (b, st), rs in per_level.items():
        if st is None or len(rs) < 2:
            continue
        vals = {round(v, 6) for _, v in rs}
        if len(vals) == 1:  # the same printed figure on two sheets of one level: counted once
            for r, _ in rs[1:]:
                r["note"] = f"same level and same value as {rs[0][0]['label']}: counted once"
        else:
            problems.append(f"two sheets for {rs[0][0]['level'] or 'one level'} with different values "
                            f"({', '.join(_fmt(v) for _, v in rs)}): which one is the plan of that level?")
    unlevelled_set = len(no_level) > 1 and all(r["storey"] is None for r, v in nums if v is not None)
    if len(no_level) > 1 and not unlevelled_set:
        problems.append(f"{len(no_level)} sheets have no level read from them: name their levels (Change level) so "
                        f"no floor is counted twice")
    # the same printed TOTAL on every sheet (a project table repeated on each floor plan): that total once (P1 c146a20)
    vals_all = [v for _, v in nums if v is not None]
    if len(vals_all) >= 2 and len({round(v, 6) for v in vals_all}) == 1 and all(
            any(re.search(r"\btotal\b", str(e.get("note", "")), re.I) for e in r["evidence"]) for r, v in nums
            if v is not None):
        problems = [x for x in problems if not x.startswith("two sheets for")]
        for r, _ in nums[1:]:
            r["note"] = "the same printed total as the first sheet: counted once"
        if nums:
            nums[0][0]["note"] = ""
    counted = [(r, v) for r, v in nums if v is not None and not r["note"]]
    # ONE named room: the level that prints its name answers; printed on several levels = which one (never a sum)
    room_note = ""
    try:
        room = plan(proj, question, current).get("room") if counted else None  # the same sheets /plan looked at
    except Exception:  # noqa: BLE001
        room = None
    if room and not problems and all(k in sheets for k in room["on"]):
        on = set(room["on"])
        lv = {(sheets[k]["building"], sheets[k]["storey"]) for k in on}
        names = ", ".join(dict.fromkeys(sheets[k]["level"] or sheets[k]["label"] for k in room["on"]))
        if len(lv) >= 2:
            problems.append(f"the question names one room ('{room['name']}') and {names} each print it: which one did "
                            f"you mean? (ask on that level's sheet)")
        elif not room["unknown"]:
            for r, _ in counted:
                if r["sheet"] not in on:
                    r["note"] = f"'{room['name']}' is not printed on this sheet"
            counted = [(r, v) for r, v in counted if not r["note"]]
            room_note = f"'{room['name']}' is printed on {names} only: that sheet's value, not a sum over levels"
    total, unit = None, None
    vertical =bool(_VERTICAL.search(question or "")) and counted and all(
        (_unit(r["unit"]) or ("?", 1))[0] == "count" for r, _ in counted)
    if vertical and not problems:
        # a lift on three plans is ONE lift: the most seen on one level, never the sum (2 + 2 + 1 lifts != 5); with
        # several buildings, the most on one level OF EACH building, added
        tops = {}
        for r, v in counted:
            b = r["building"] if several else ""
            if b not in tops or v > tops[b][1]:
                tops[b] = (r, v)
        top_r, top_v = max(tops.values(), key=lambda rv: rv[1])
        tot = sum(v for _, v in tops.values())
        total, unit = int(tot) if float(tot).is_integer() else tot, top_r["unit"]
        top_ids = {id(r) for r, _ in tops.values()}
        for r, _ in counted:
            r["counted"] = id(r) in top_ids
            r["note"] = "" if r["counted"] else "not added: the same lifts / stairs pass through every level"
        each = ", ".join(f"{_fmt(v)} on {r['level'] or r['label']}" for r, v in counted)
        largest = " + ".join(f"{_fmt(v)} on {r['level'] or r['label']}" for r, v in tops.values())
        ev = [dict(e, note=f"{r['level'] or r['label']}: {e.get('note') or ''}".rstrip(": "))
              for r, _ in tops.values() for e in r["evidence"]]
        return {"value": total, "unit": unit, "confidence": round(min(0.5, top_r["confidence"]), 3), "evidence": ev,
                "solver": "project/sheets", "scope": "building", "building": target or "",
                "trace": [f"project: stairs / lifts / shafts / columns run through several levels: counts per level "
                          f"({each}) are not added; the building count is the most on one level = {largest}"
                          + (f" = {_fmt(total)}" if len(tops) > 1 else "")],
                "assumptions": [f"vertical elements appear on every level they serve: per level {each}; the building "
                                f"count is the largest ({largest}"
                                + (" per building" if len(tops) > 1 else "") + "), assuming each one reaches that level"],
                "missing": ["which levels each lift / stair serves (a section or the lift schedule would confirm)"],
                "cost": _sum_cost([p.get("answer") or {} for p in parts]),
                "per_sheet": [{k: v for k, v in r.items() if k != "evidence"} | {"n_evidence": len(r["evidence"])}
                              for r in rows], "no_total_why": ""}
    if not problems and counted:
        base = _unit(counted[0][0]["unit"])
        if base and all(_unit(r["unit"]) for r, _ in counted):
            same_unit = len({str(r["unit"]).lower() for r, _ in counted}) == 1
            if same_unit:
                total, unit = sum(v for _, v in counted), counted[0][0]["unit"]
            else:
                total, unit = sum(v * _unit(r["unit"])[1] for r, v in counted), base[0]
        else:
            total, unit = sum(v for _, v in counted), counted[0][0]["unit"]
        if unit in ("count", None, "") and total is not None and all(float(v).is_integer() for _, v in counted):
            total = int(round(total))
        for r, _ in counted:
            r["counted"] = True
    each = " + ".join(f"{_fmt(v)} on {r['level'] or r['label']}" for r, v in counted)
    ev = []
    for r in rows:
        if not r["counted"]:
            continue
        for e in r["evidence"]:
            e = dict(e)
            e["note"] = f"{r['level'] or r['label']}: {e.get('note') or ''}".rstrip(": ")
            ev.append(e)
    conf = min([TOTAL_CAP] + [r["confidence"] for r, _ in counted]) if total is not None else 0.0
    if total is not None and unlevelled_set:
        # pictures before their text is read, or plans printing no level: a best estimate, said plainly (Stoa: an
        # answer with its assumption beats a refusal); copies of one sheet were already left out by plan()
        conf = min(conf, 0.35)
    if total is not None:
        trace = [f"project: building-level answer over {len(counted)} sheet(s): {each} = {_fmt(total)}"]
        assumptions = [room_note] if room_note else [f"one plan per level, summed: {each}"]
        skipped = [r for r in rows if not r["counted"] and r["note"]]
        assumptions += [f"{r['label']}: {r['note']}" for r in skipped]
        if unlevelled_set:
            assumptions.insert(0, f"no level is printed on (or read yet from) these {len(counted)} sheets: each is "
                                  f"taken as a different floor; name their levels under Change if two show the same floor")
        else:
            assumptions += [f"{r['label']}: no level read from it; counted as a level of its own (name its level if "
                            f"it repeats another)" for r, _ in counted if r["storey"] is None]
        gross = [r for r, _ in counted if r["solver"] == "project/gross_area"]
        if gross:
            assumptions += [f"{r['level'] or r['label']}: {(r['assumptions'] or ['the gross internal area of its outline'])[0]}"
                            for r in gross[:1]]
            assumptions += [f"{r['level'] or r['label']}: {x}" for r in gross for x in r["assumptions"] if x.startswith("cross-check")]
        summed_rooms = [r for r, _ in counted if r["solver"] == "project/rooms_sum"]
        if summed_rooms:  # whole-floor areas: say how each level's number was made
            assumptions += [f"{r['level'] or r['label']}: {(r['assumptions'] or ['the sum of its traced rooms'])[0]}"
                            for r in summed_rooms]
            assumptions.append("walls are not included in a level's sum of rooms (a gross internal area would be larger)")
        missing_l: list[str] = []
    else:
        trace = ["project: no building total: " + "; ".join(problems or ["no sheet gave a number"])]
        assumptions = []
        missing_l = problems or ["no sheet gave a number"]
    answer = {"value": total, "unit": unit, "confidence": round(conf, 3), "evidence": ev, "solver": "project/sheets",
              "trace": trace, "assumptions": assumptions, "missing": missing_l,
              "cost": _sum_cost([p.get("answer") or {} for p in parts]), "scope": "building", "building": target or "",
              "per_sheet": [{k: v for k, v in r.items() if k != "evidence"} | {"n_evidence": len(r["evidence"])}
                            for r in rows],
              "no_total_why": "; ".join(problems) if total is None else ""}
    if total is not None and several:  # 'Special School 95 + 83 = 178; Primary School 120' before the grand total
        by_b: dict = {}
        for r, v in counted:
            by_b[r["building"]] = by_b.get(r["building"], 0) + v * ((_unit(r["unit"]) or ("", 1.0))[1] if unit != r["unit"] else 1)
        answer["per_building"] = [{"building": b, "value": int(v) if float(v).is_integer() else round(v, 3), "unit": unit}
                                  for b, v in by_b.items()]
        answer["assumptions"].insert(0, "several buildings, added: " + "; ".join(
            f"{b} {_fmt(v)}" for b, v in by_b.items()) + f" = {_fmt(total)}")
    elif target and total is not None:
        answer["assumptions"].insert(0, b_why if b_why.startswith("no building in this upload") else
                                     f"{target} only (the other buildings of this project are not added)")
    return answer


def _fmt(v) -> str:
    v = float(v)
    return f"{int(v):,}" if v.is_integer() else f"{v:,.2f}"


def _sum_cost(answers: list[dict]) -> dict:
    keys = ("llm_tokens_in", "llm_tokens_out", "gpu_seconds", "api_calls", "wall_seconds")
    out = {k: 0.0 for k in keys}
    for a in answers:
        c = a.get("cost") or {}
        for k in keys:
            try:
                out[k] += float(c.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return {k: (int(v) if k in ("llm_tokens_in", "llm_tokens_out", "api_calls") else round(v, 3)) for k, v in out.items()}


# ---------------------------------------------------------------------------------------------------- review list
def _norm_q(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(q or "").lower()).strip()


# 'Several rooms carry the asked name and the answer is their mean' (E1's lineplan: "6 rooms are labelled 'Secondary
# Classroom' and measure alike (52.13, 52.29, …): their mean"): a real ambiguity only the user can settle. The item is an
# OPEN question with one choice per numbered mark (the card numbers its evidence 1..N in this order); the user's pick is
# saved like a correction (and sent to F7), 'Keep the mean' marks it resolved.
_SAME_LABEL = re.compile(r"(\d+)\s+rooms\s+are\s+labell?ed\s+'([^']+)'", re.I)
_EV_VALUE = re.compile(r"^\s*(?P<label>[^:]{1,60}):\s*(?P<v>\d[\d,]*(?:\.\d+)?)\s*(?P<u>m²|m2|m|ft²|sf|sq\s?ft|ft)(?![a-z])",
                       re.I)
_UNIT_NAME = {"m²": "m2", "m2": "m2", "m": "m", "ft²": "ft2", "sf": "ft2", "sq ft": "ft2", "sqft": "ft2", "ft": "ft"}


_SUBQ = re.compile(r"^[^—]{8,240}\?\s+—\s+")


def _ambiguity(answer: dict) -> dict | None:
    m = _SAME_LABEL.search(" ".join(str(a) for a in answer.get("assumptions") or []))
    if not m or answer.get("value") is None:
        return None
    choices = []
    for i, e in enumerate(answer.get("evidence") or []):
        # with the plug-ins on, a note can start with the planner's sub-question ('What is the area of …? — '; W5 22:44)
        mm = _EV_VALUE.match(_SUBQ.sub("", str(e.get("note") or "")))
        if mm:
            choices.append({"n": i + 1, "label": mm["label"].strip(), "value": float(mm["v"].replace(",", "")),
                            "unit": _UNIT_NAME.get(mm["u"].lower().replace("  ", " "), mm["u"]), "page": e.get("page"),
                            "bbox": e.get("bbox"), "bbox_space": e.get("bbox_space")})
    if len(choices) < 2:
        return None
    v = answer["value"]
    return {"label": m[2], "n": int(m[1]), "choices": choices[:12],
            "why": f"{m[1]} rooms are labelled '{m[2]}': the answer {_fmt(v) if _num(v) is not None else v} is their "
                   f"mean. Which one did you mean? (the numbers match the marks on the drawing)"}


def _item_from(question: str, answer: dict, scope: str, file: str = "", page: int = 0, label: str = "",
               level: str = "") -> dict:
    v = answer.get("value")
    ev = [dict(e) for e in (answer.get("evidence") or [])[:20]]
    conf = float(answer.get("confidence") or 0)
    amb = _ambiguity(answer) if scope == "sheet" else None
    open_ = v is None or (isinstance(v, str) and not v.strip()) or conf < OPEN_BELOW or amb is not None
    return {"id": uuid.uuid4().hex[:10], "question": str(question).strip(), "q_norm": _norm_q(question), "scope": scope,
            "file": file, "page": int(page or 0), "sheet": label, "level": level, "value": v,
            "unit": answer.get("unit"), "confidence": float(answer.get("confidence") or 0),
            "solver": answer.get("solver", ""), "evidence": ev,
            # the web sends the first 20 boxes + how many the answer had (M1 22:35: '115 doors · 20 marked')
            "n_evidence": max(int(answer.get("n_evidence") or 0), len(answer.get("evidence") or [])),
            "per_sheet": answer.get("per_sheet"),
            "why_open": (amb["why"] if amb else _why_open(answer)) if open_ else "", "ambiguous": amb,
            "status": "open" if open_ else "answered", "checked": False, "corrected": None,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _why_open(answer: dict) -> str:
    if answer.get("value") is None:
        return (answer.get("no_total_why") or "; ".join(answer.get("missing") or []) or "no answer")[:300]
    return (f"only a rough guess ({_fmt(answer['value']) if _num(answer['value']) is not None else answer['value']}"
            f"{' ' + str(answer.get('unit')) if answer.get('unit') not in (None, '', 'count') else ''}, "
            f"confidence {float(answer.get('confidence') or 0):.2f}): check it on the drawing or give the value")[:300]


def upsert_item(proj: dict, item: dict) -> dict:
    """The same question on the same sheet (or the building) again: the newer answer replaces the older; a tick or a
    correction the user made stays when the value is unchanged."""
    items = proj.setdefault("items", [])
    for i, old in enumerate(items):
        if old.get("q_norm") == item["q_norm"] and old.get("scope") == item["scope"] and (
                item["scope"] == "building" or (old.get("file") == item["file"] and old.get("page") == item["page"])):
            item["id"] = old["id"]
            item["created"] = old.get("created", item["created"])
            c = old.get("corrected") or {}
            if c and _num(item.get("value")) is not None and _num(c.get("value")) is not None and \
                    abs(_num(item["value"]) - _num(c["value"])) < 1e-9:
                return old  # the user's own value coming back (F7 applied it): keep the original reading + tick
            if old.get("value") == item.get("value"):
                item["checked"], item["corrected"] = old.get("checked", False), old.get("corrected")
                if old.get("user_resolved"):  # 'Keep the mean' / 'Mark resolved' stays for the same reading
                    item["user_resolved"], item["status"], item["why_open"] = True, "answered", old.get("why_open", "")
            elif old.get("corrected"):
                item["corrected"] = old["corrected"]  # the user's value stays the project's value
            if item["corrected"] and item["status"] == "open":
                item["status"] = "answered"
            items[i] = item
            return item
    items.append(item)
    return item


def patch_item(proj: dict, iid: str, patch: dict) -> dict:
    for it in proj.get("items", []):
        if it["id"] != iid:
            continue
        if "checked" in patch:
            it["checked"] = bool(patch["checked"])
        if "value" in patch:
            val = patch["value"]
            if val in (None, ""):
                it["corrected"] = None
                if it.get("value") is None:
                    it["status"] = "open"
            else:
                it["corrected"] = {"value": _num(val) if _num(val) is not None else str(val),
                                   "unit": patch.get("unit") or it.get("unit"), "note": str(patch.get("note") or "")[:300],
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                it["checked"] = True
                it["status"] = "answered"
                it["f7"] = _to_f7(it, proj.get("visitor") or "")
        if patch.get("resolved") is True and it.get("status") == "open":
            it["status"], it["user_resolved"] = "answered", True
            it["why_open"] = (it.get("why_open") or "") + (" (you kept the mean)" if it.get("ambiguous") else
                                                           " (marked resolved)")
        if patch.get("resolved") is False:
            it["status"], it["user_resolved"] = "open", False
        return it
    raise KeyError(iid)


def _f7():
    try:
        from stoa.features import corrections

        return corrections if hasattr(corrections, "add") else None
    except Exception:  # noqa: BLE001
        return None


def _to_f7(it: dict, visitor: str = "") -> str:
    """A corrected this-sheet value goes to F7's memory too (the same question on that file answers it, labelled as
    the user's); building totals stay in the project only. Returns the F7 item id or '' (fail closed)."""
    mod = _f7()
    if mod is None or it.get("scope") != "sheet" or not it.get("file"):
        return ""
    try:
        c = it["corrected"]
        got = mod.add(it["file"], "value", {"question": it["question"], "value": c["value"], "unit": c.get("unit"),
                                            "page": it.get("page"), "note": c.get("note") or "project review list",
                                            **({"visitor": visitor} if visitor else {})})  # F7 may scope by it
        return str(got.get("id", ""))
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------------------------------- whole-floor areas
# 'What is the total floor area of the school?' asked per sheet goes to the general readers, which guess (DfE SEN L00
# 4093 m², L01 980 m², confidence 0.05, 'no PDF reader could read this sheet' on a vector PDF). A level's NET floor area
# is the sum of its rooms: every room label traced from the wall lines around it (E1's lineplan tracer via F2's
# room_labels; DfE SEN L00 78 of 80 labels in ~18 s, L01 38 of 38 in ~5 s), a room carrying two labels counted once,
# each room's outline as evidence. Walls are not included: said as an assumption (a gross area would be larger).
_FLOOR_AREA_Q = re.compile(
    r"\b(?:gross|net)\s+(?:internal\s+)?(?:floor\s+)?area\b|\b(?:GIA|GIFA|NIA)\b|"
    r"\b(?:total|overall|whole|entire)\s+(?:floor\s+)?area\b(?!\s+of\s+(?:all\s+|the\s+|each\s+)?(?!rooms?\b|spaces?\b|"
    r"building|school|house|home|project|plan|floor|level|storey|sheet|drawing|premises|facility)\w)|"
    r"\barea\s+of\s+all\s+(?:the\s+)?(?:rooms|spaces)\b|"
    r"\bfloor\s+area\s+(?:of|on|in)\s+(?:the\s+|this\s+)?(?:whole\s+|entire\s+)?(?:building|school|house|project|plan|"
    r"floor|level|storey|sheet|drawing)\b", re.I)
_FA_CACHE: dict = {}
_FA_LOCK = threading.Lock()


def is_floor_area_question(question: str) -> bool:
    return bool(_FLOOR_AREA_Q.search(str(question or "")))


_GIFA_HEAD = re.compile(r"^(?:GIFA|GIA|NIA|GEA)$|^gross$", re.I)
_AREA_TOKEN = re.compile(r"^(\d[\d,]*(?:\.\d+)?)\s*(m2|m²|sqm|ft2|ft²|sf)?$", re.I)


def printed_floor_areas(abs_path: str, page: int = 0) -> dict | None:
    """A printed floor-area table ('Overall GIFA  Ground: 2398m2  First: 1172m2  Second: 1274m2  Total: 4844m2' on
    the EFA baseline sheets) -> {kind, levels: {storey: (value, unit, bbox)}, total: (value, unit, bbox) | None}."""
    try:
        import pymupdf

        with pymupdf.open(abs_path) as d:
            words = d[int(page)].get_text("words")
    except Exception:  # noqa: BLE001
        return None
    for h in words:
        if not _GIFA_HEAD.match(h[4]):
            continue
        kind = h[4].upper() if h[4].upper() != "GROSS" else "GIA"
        below = [w for w in words if h[1] + 2 < w[1] < h[1] + 70 and h[0] - 60 < w[0] < h[0] + 160]
        rows: dict = {}
        for w in below:
            rows.setdefault(round(w[1] / 3), []).append(w)
        levels, total = {}, None
        for _, ws in sorted(rows.items()):
            ws.sort(key=lambda w: w[0])
            label = " ".join(w[4] for w in ws if not re.search(r"\d", w[4])).strip(" :").lower()
            nums = [w for w in ws if _AREA_TOKEN.match(w[4].replace(" ", ""))]
            if not label or not nums:
                continue
            m = _AREA_TOKEN.match(nums[0][4])
            unit = (m.group(2) or next((w[4] for w in ws if w[4].lower() in ("m2", "m²", "sqm")), "m2")).lower()
            val = (float(m.group(1).replace(",", "")), "m2" if unit in ("m2", "m²", "sqm") else "ft2",
                   [round(min(w[0] for w in ws), 1), round(min(w[1] for w in ws), 1), round(max(w[2] for w in ws), 1),
                    round(max(w[3] for w in ws), 1)])
            if re.match(r"^(?:total|overall|building)\b", label):
                total = val
                continue
            if re.search(r"target|required|brief", label):
                continue
            k, _ = level_from_name(re.sub(r"\s+", "_", label) + ("_floor" if re.fullmatch(
                r"ground|first|second|third|fourth|fifth|lower ground|upper ground", label) else "") + ".x")
            if k is not None:
                levels[k] = val
        if levels or total:
            return {"kind": kind, "levels": levels, "total": total}
    return None


_NET_Q = re.compile(r"\bnet\b|\bnia\b|\busable\b|\bcarpet\b|\b(?:all|every|each)\s+(?:the\s+)?(?:rooms?|spaces?)\b|"
                    r"\bsum\s+of\s+(?:the\s+|all\s+)?rooms\b|\brooms?\s+(?:added|summed)\b", re.I)


def _gross_area(abs_path: str, virtual: str, page: int) -> dict | None:
    """VP's stoa.sheet.footprint: the gross INTERNAL area (RICS GIA / GIFA) from the drawn building outline, inner face
    of the outer walls, voids left out ($0, ~0.3 s; a picture only through its already-traced copy). VP: 8/8 EFA
    solid-wall plans within 3.5 % of the printed GIFA. None when the module is absent or reads nothing usable."""
    try:
        from stoa.sheet.footprint import footprint_answer
    except Exception:  # noqa: BLE001 - VP's head not on this branch yet
        return None
    try:
        a = footprint_answer(abs_path, "What is the gross internal floor area of this floor?", int(page))
    except Exception:  # noqa: BLE001
        return None
    if a is None or a.value is None or float(a.confidence or 0) < 0.3:
        return None
    d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
    for e in d.get("evidence") or []:
        e["file"] = virtual
    d.update({"solver": "project/gross_area", "fallback": False, "unit": d.get("unit") or "m2"})
    d["assumptions"] = ["gross internal floor area (GIA): the drawn building outline to the inner face of its outer "
                        "walls, internal walls / stairs / shafts included, voids left out"] + list(d.get("assumptions") or [])
    return d


def floor_area(abs_path: str, virtual: str, page: int = 0, storey: str | None = None, question: str = "") -> dict:
    """A sheet's floor area (Answer-shaped): a PRINTED floor-area table first (its line for the sheet's own level),
    then (unless the question asks for a NET / rooms area) the gross internal area of the drawn outline (VP's
    footprint) with the traced rooms' net sum as a cross-check when it is already known, else the net sum of its
    traced rooms. value None when nothing can be read (a picture, no printed scale, < half of the labels traced):
    then the caller asks the pipeline instead."""
    st = os.stat(abs_path)
    net = bool(_NET_Q.search(question or ""))
    key = (abs_path, int(page), st.st_size, int(st.st_mtime), storey, net)
    rk = (abs_path, int(page), st.st_size, int(st.st_mtime))

    def cross(out: dict) -> dict:  # the gross figure + the rooms' net sum once that is known (traced ~5-35 s)
        with _FA_LOCK:
            rooms = _FA_CACHE.get(rk)
        if out.get("solver") == "project/gross_area" and "net_m2" not in out and rooms and rooms.get("value"):
            g, n_ = float(out["value"]), float(rooms["value"])
            out["assumptions"].insert(1, f"cross-check: its {rooms.get('rooms')} traced rooms add up to {n_:,.2f} m² net "
                                      f"({100 * n_ / g:.0f} % of the gross; the rest is walls, ducts and untraced spaces)")
            out["net_m2"] = n_
        return out

    with _FA_LOCK:
        hit = _FA_CACHE.get(key)
    if hit is not None:
        return cross(hit)  # (outside the lock: cross takes it, and it is not re-entrant)
    out = _printed_area(abs_path, virtual, page, storey)
    if out is None and not net:
        out = _gross_area(abs_path, virtual, page)
        if out is not None:
            out = cross(out)
    if out is None:
        out = _rooms_sum(abs_path, virtual, page, rk)
    out = _scope_of_area(out)
    if out.get("value") is not None:
        with _FA_LOCK:
            _FA_CACHE[key] = out
    return out


def _scope_of_area(out: dict) -> dict:
    """What the floor area includes / leaves out, in the fields the card reads first (web customer.ts scopeOf): the
    card said 'Net internal area, to the inner wall face' (its default convention) under a GROSS internal area."""
    if out.get("value") is None or out.get("includes"):
        return out
    sv = out.get("solver")
    conv = ""
    if sv == "project/gross_area":
        conv = "gross internal area (GIA)"
        out["includes"] = ["internal walls, stairs and shafts"]
        out["excludes"] = ["the outer walls", "voids"]
    elif sv == "project/rooms_sum":
        conv = "net: the sum of the traced rooms"
        out["includes"] = ["the traced rooms"]
        out["excludes"] = ["walls", "untraced spaces"]
    elif sv == "project/printed_area":
        conv = "as printed in the sheet's own area table"
    if conv:  # the card's 'Scope:' takes a 'convention:' line first (else its room default 'net internal area')
        out["trace"] = [f"convention: {conv}", *list(out.get("trace") or [])]
    return out


def _printed_area(abs_path: str, virtual: str, page: int, storey: str | None) -> dict | None:
    """The sheet's printed floor-area table line for its own level (EFA 'Overall GIFA'), or None. VP's
    stoa.sheet.footprint.printed_gross first (one reader for printed GIFA: also US 'TOTAL SECOND FLOOR = 26,562 SF'
    rows, and pictures through their already-cached text layer); my own table reader when it reads nothing."""
    pdf = abs_path.lower().endswith(".pdf")
    if storey is not None:
        try:
            from stoa.sheet.footprint import printed_gross

            layer = None if pdf else _image_layer(abs_path)
            rows = printed_gross(abs_path, page, layer=layer) if (pdf or layer is not None) else []
        except Exception:  # noqa: BLE001 - VP's module absent / failed: my reader below
            rows = []
        own = [r for r in rows if r.get("kind") == "storey" and r.get("storey") == storey and not r.get("net")]
        if own and len({(r["value"], r["unit"]) for r in own}) == 1:
            r = own[0]
            tot = next((t for t in rows if t.get("kind") == "total" and not t.get("net")), None)
            v, u = float(r["value"]), r["unit"]
            out = {"missing": [], "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0}}
            out.update({"value": v, "unit": u, "confidence": 0.8 if pdf else 0.6, "solver": "project/printed_area",
                        "fallback": False,
                        "evidence": [{"file": virtual, "page": int(page), "bbox": r["bbox"],
                                      "bbox_space": "pdf_pt" if pdf else "image_px",
                                      "note": f"printed for this level: {r.get('text') or f'{v:g} {u}'}"}],
                        "assumptions": [f"the floor area printed on the sheet for this level ({r.get('text') or f'{v:g} {u}'})"
                                        + (f"; the sheet's printed total is {float(tot['value']):g} {tot['unit']}" if tot else "")
                                        + ("" if pdf else "; read from the picture's text, check the digits")],
                        "trace": [f"printed_area: VP printed_gross row '{r.get('label')}' for level {storey} = {v:g} {u}"],
                        "printed_total": float(tot["value"]) if tot else None})
            return out
    if pdf:
        pr = printed_floor_areas(abs_path, page)
        if pr and storey is not None and storey in pr["levels"]:
            v, u, bb = pr["levels"][storey]
            out = {"missing": [], "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0}}
            out.update({"value": v, "unit": u, "confidence": 0.8, "solver": "project/printed_area", "fallback": False,
                        "evidence": [{"file": virtual, "page": int(page), "bbox": bb, "bbox_space": "pdf_pt",
                                      "note": f"printed {pr['kind']} for this level: {v:g} {u}"}],
                        "assumptions": [f"the {pr['kind']} printed on the sheet for this level ({v:g} {u})"
                                        + (f"; the sheet's printed total is {pr['total'][0]:g} {pr['total'][1]}"
                                           if pr.get("total") else "")],
                        "trace": [f"printed_area: {pr['kind']} table, level {storey} = {v:g} {u}"],
                        "printed_total": pr["total"][0] if pr.get("total") else None})
            return out
    return None


def _rooms_sum(abs_path: str, virtual: str, page: int, key) -> dict:
    """The net sum of the sheet's traced rooms (cached under key = (path, page, size, mtime))."""
    with _FA_LOCK:
        if key in _FA_CACHE:
            return _FA_CACHE[key]
    t0 = time.time()
    out = {"value": None, "unit": "m2", "confidence": 0.0, "evidence": [], "solver": "project/rooms_sum",
           "assumptions": [], "missing": [], "trace": [], "fallback": True}
    try:
        from stoa.evidence import lineplan
        from stoa.features import takeoff as T
    except Exception as e:  # noqa: BLE001
        out["missing"] = [f"room tracer not on this server ({type(e).__name__})"]
        return out
    src, px, how = abs_path, None, ""
    if abs_path.lower().endswith((".png", ".jpg", ".jpeg")):
        # a picture: P1's traced vector copy (cached next to the upload; ~20 s the first time), read like a PDF, its
        # evidence mapped back to the picture's pixels (F2's takeoff.traced). X1's light scan of DfE SEN L00: 1574.1 m²
        # over 67 rooms vs 1606.37 m² on the CAD PDF (2 %)
        try:
            tr = T.traced(abs_path)
        except Exception:  # noqa: BLE001
            tr = None
        if tr is None:
            out["missing"] = ["a picture whose paper size / DPI could not be read: its lines were not traced"]
            return out
        src, px, how = tr[0], tr[1], tr[2]
        page = 0
    elif not abs_path.lower().endswith(".pdf"):
        out["missing"] = ["not a drawing sheet"]
        return out
    n = T._scale(src, page)
    labs = [r for r in T.room_labels(src, page) if not T._VOID_RX.search(r["name"])]
    if not n or len(labs) < 3:
        out["missing"] = ["no printed scale on the sheet" if not n else "fewer than 3 room labels on the sheet"]
        return out
    seen, rooms, untraced = set(), [], []
    for r in labs:
        try:
            m = lineplan._measure(src, page, tuple(r["pt"]), n, tuple(r["bbox"]))
        except Exception:  # noqa: BLE001
            m = None
        if not m or not m.get("area_m2"):
            untraced.append(r["name"])
            continue
        k = tuple(round(c) for p in m["polygon"] for c in p)
        if k in seen:  # two labels inside one room ('Store' + its number): that room once
            continue
        seen.add(k)
        rooms.append((r, m))
    if len(rooms) < max(3, 0.5 * len(labs)):
        out["missing"] = [f"only {len(rooms)} of {len(labs)} room labels could be traced"]
        out["trace"] = [f"rooms_sum: {len(rooms)} of {len(labs)} labels traced: too few for a floor total"]
        return out
    total = round(sum(m["area_m2"] for _, m in rooms), 2)
    conf = 0.55 if not untraced else 0.5 if len(untraced) <= max(2, 0.05 * len(labs)) else 0.35
    ev = []
    for r, m in sorted(rooms, key=lambda rm: -rm[1]["area_m2"]):
        poly = m["polygon"]
        xs, ys = [q[0] for q in poly], [q[1] for q in poly]
        ev.append({"file": virtual, "page": int(page), "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1),
                                                                round(max(ys), 1)], "bbox_space": "pdf_pt",
                   "polygon": [[round(x, 2), round(y, 2)] for x, y in poly],
                   "note": f"{r['name']}: {m['area_m2']:.2f} m² traced at 1:{n:g}"})
    if px:  # the picture's own pixels
        for e in ev:
            e.update({"bbox": [round(v * px, 1) for v in e["bbox"]], "bbox_space": "image_px", "page": None,
                      "polygon": [[round(x * px, 1), round(y * px, 1)] for x, y in e["polygon"]]})
        conf = min(conf, 0.4)
    biggest = ", ".join(f"{r['name']} {m['area_m2']:.1f}" for r, m in sorted(rooms, key=lambda rm: -rm[1]["area_m2"])[:3])
    out.update({
        "value": total, "confidence": conf, "evidence": ev, "fallback": False,
        "assumptions": [f"net floor area: the sum of {len(rooms)} rooms traced from the wall lines around their labels "
                        f"at the printed 1:{n:g} (largest: {biggest} m²)",
                        "walls are not included (a gross internal area would be larger); corridors, stores and "
                        "covered walkways with a label are"]
                       + ([f"{len(untraced)} label(s) could not be traced and are not in the sum: "
                           + ", ".join(sorted(set(untraced)))[:200]] if untraced else [])
                       + ([f"read on the picture's lines traced into a vector drawing ({how}); room names as OCR "
                           f"read them"] if px else []),
        "missing": ["a printed area schedule or GIA figure would confirm the total"],
        "trace": [f"rooms_sum: {len(rooms)} rooms of {len(labs)} labels at 1:{n:g} = {total} m² "
                  f"({time.time() - t0:.1f} s)"],
        "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0,
                 "wall_seconds": round(time.time() - t0, 2)},
        "rooms": len(rooms), "labels": len(labs)})
    with _FA_LOCK:
        _FA_CACHE[key] = out
    return out


# ----------------------------------------------------------------------------------------------------------- export
CSV_COLS = ["scope", "level", "sheet", "page", "question", "value", "unit", "confidence", "checked", "corrected_value",
            "corrected_unit", "correction_note", "status", "why_open", "solver", "evidence_note", "evidence_box", "file"]


def rows_for_export(proj: dict) -> list[dict]:
    out = []
    for it in proj.get("items", []):
        ev = (it.get("evidence") or [{}])[0] if it.get("evidence") else {}
        c = it.get("corrected") or {}
        out.append({"scope": "this building" if it["scope"] == "building" else "this sheet", "level": it.get("level", ""),
                    "sheet": it.get("sheet", ""), "page": (it.get("page") or 0) + 1 if it["scope"] == "sheet" else "",
                    "question": it["question"], "value": "" if it.get("value") is None else it["value"],
                    "unit": it.get("unit") or "", "confidence": it.get("confidence", ""),
                    "checked": "yes" if it.get("checked") else "", "corrected_value": c.get("value", ""),
                    "corrected_unit": c.get("unit", "") if c else "", "correction_note": c.get("note", ""),
                    "status": it.get("status", ""), "why_open": it.get("why_open", ""), "solver": it.get("solver", ""),
                    "evidence_note": ev.get("note", ""),
                    "evidence_box": " ".join(f"{float(x):.0f}" for x in ev.get("bbox") or []),
                    "file": ev.get("file") or it.get("file", "")})
    return out


def to_csv(proj: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS)
    w.writeheader()
    for r in rows_for_export(proj):
        w.writerow(r)
    return buf.getvalue()


def per_level_rows(proj: dict) -> tuple[list[str], list[list]]:
    """Every building answer split by level (the sheet each value came from, added or not and why)."""
    head = ["question", "building", "level", "sheet", "value", "unit", "confidence", "added", "note"]
    body = []
    for it in proj.get("items", []):
        for r in it.get("per_sheet") or []:
            b = r.get("building") or ""
            lv = str(r.get("level", ""))
            lv = lv[len(b) + 3:] if b and lv.startswith(f"{b} · ") else lv
            body.append([it["question"], b, lv, r.get("label", ""), "" if r.get("value") is None else r["value"],
                         r.get("unit") or "", r.get("confidence", ""), "yes" if r.get("counted") else "no", r.get("note", "")])
    return head, body


def to_xlsx(proj: dict) -> bytes | None:
    """F2's dependency-free XLSX writer (stoa.features.takeoff: its two-sheet workbook with our two tables in it:
    'Review' and 'Per level'); None when F2's writer is not on the tree or its layout differs (the web offers CSV)."""
    try:
        import zipfile

        from stoa.features import takeoff as T

        base = T.to_xlsx([], title="Review")
        rows = rows_for_export(proj)
        head, body = CSV_COLS, [[r[k] for k in CSV_COLS] for r in rows]
        lhead, lbody = per_level_rows(proj)
        src = zipfile.ZipFile(io.BytesIO(base))
        names = src.namelist()
        if "xl/worksheets/sheet1.xml" not in names or "xl/worksheets/sheet2.xml" not in names:
            return None
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                data = src.read(n)
                if n == "xl/worksheets/sheet1.xml":
                    data = T._sheet_xml(head, body).encode()
                elif n == "xl/worksheets/sheet2.xml":
                    data = T._sheet_xml(lhead, lbody).encode()
                elif n == "xl/workbook.xml":
                    data = data.replace(b'name="Details"', b'name="Per level"')
                z.writestr(n, data)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - CSV stays available
        return None


# ----------------------------------------------------------------------------------------------------------- router
def _open(files: list[str], name: str = "", refresh: bool = False, visitor: str = "") -> dict:
    files = [str(f) for f in files if str(f).strip()]
    if not files:
        raise ValueError("a project needs at least one file")
    pid = project_id(files)
    with _LOCK:
        old = load(pid, visitor)
        if old and not refresh and sorted(old.get("files", [])) == sorted(files) and old.get("sheets"):
            # re-read sheets nobody could read before (an image whose text layer has landed since), and projects
            # stored by an older reader (VERSION 2: several buildings)
            if old.get("version") != VERSION or any(s.get("kind_source") == "not read" or s.get("storey") is None
                                                    for s in old["sheets"]):
                proj = build(files, name, old)
                return save(pid, proj, visitor)
            if name and name != old.get("name"):
                old["name"], old["name_user"] = name, True
                return save(pid, old, visitor)
            if not old.get("name_user"):  # a better automatic name since (copies left out of it)
                auto = _guess_name(old["sheets"], files)
                if auto != old.get("name"):
                    old["name"] = auto
                    return save(pid, old, visitor)
            return old
        return save(pid, build(files, name, old), visitor)


def _get(pid: str, visitor: str = "") -> dict:
    p = load(_check_pid(pid), visitor)
    if p is None:
        raise KeyError(pid)
    return p


try:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import Response
    from pydantic import BaseModel

    router = APIRouter(prefix="/feat/project", tags=["project"])

    class OpenIn(BaseModel):
        files: list[str]
        name: str = ""
        refresh: bool = False

    class PlanIn(BaseModel):
        question: str
        current: str = ""  # the sheet on screen ('file#page'): its building, when the project has several

    class CombineIn(BaseModel):
        question: str
        parts: list[dict]
        current: str = ""

    class ItemIn(BaseModel):
        question: str
        file: str = ""
        page: int = 0
        answer: dict
        scope: str = "sheet"

    def _err(e: Exception):
        if isinstance(e, KeyError):
            raise HTTPException(404, f"no such project / item: {e}") from e
        if isinstance(e, FileNotFoundError):
            raise HTTPException(404, f"file not found (uploads live in a temp folder): {e}") from e
        raise HTTPException(400, str(e)) from e

    @router.post("/open")
    def open_project(req: OpenIn, visitor: str = "") -> dict:
        try:
            return view(_open(req.files, req.name, req.refresh, visitor))
        except (ValueError, KeyError, FileNotFoundError) as e:
            _err(e)

    @router.get("/{pid}")
    def get_project(pid: str, visitor: str = "") -> dict:
        try:
            return view(_get(pid, visitor))
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/plan")
    def plan_route(pid: str, req: PlanIn, visitor: str = "") -> dict:
        try:
            return plan(_get(pid, visitor), req.question, req.current)
        except (ValueError, KeyError) as e:
            _err(e)

    class RouteIn(BaseModel):
        question: str
        current: str = ""

    @router.post("/{pid}/route")
    def route_route(pid: str, req: RouteIn, visitor: str = "") -> dict:
        try:
            return {"sheet": route(_get(pid, visitor), req.question, req.current)}
        except (ValueError, KeyError) as e:
            _err(e)

    class FloorIn(BaseModel):
        sheet: str
        question: str = ""  # 'net' / 'usable' / 'all rooms' -> the traced rooms' sum; else the gross internal area

    @router.post("/{pid}/floor_area")
    def floor_area_route(pid: str, req: FloorIn, visitor: str = "") -> dict:
        """One sheet's net floor area as the sum of its traced rooms (Answer-shaped; value None + fallback = ask the
        pipeline instead)."""
        try:
            proj = _get(pid, visitor)
            sh = next((s for s in proj.get("sheets", []) if s["id"] == req.sheet), None)
            if sh is None:
                raise KeyError(req.sheet)
            e = effective(sh)
            return floor_area(resolve(sh["file"]), sh["file"], int(sh["page"]), e.get("storey"), req.question)
        except (ValueError, KeyError, FileNotFoundError) as e:
            _err(e)

    @router.post("/{pid}/combine")
    def combine_route(pid: str, req: CombineIn, visitor: str = "") -> dict:
        try:
            with _LOCK:
                proj = _get(pid, visitor)
                ans = combine(proj, req.question, req.parts, req.current)
                levels = ", ".join(r["level"] or r["label"] for r in ans["per_sheet"] if r["counted"])
                if ans.get("building") and levels:
                    levels = f"{ans['building']} · {levels}"
                item = upsert_item(proj, _item_from(req.question, ans, "building", label=f"{len(req.parts)} sheets",
                                                    level=levels or "whole building"))
                save(pid, proj, visitor)
            return {"answer": ans, "item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items")
    def add_item(pid: str, req: ItemIn, visitor: str = "") -> dict:
        try:
            with _LOCK:
                proj = _get(pid, visitor)
                sid = f"{req.file}#{int(req.page)}"
                es = effective_all(proj.get("sheets", []))
                sh = next((s for s in es if s["id"] == sid), None)
                if sh is None:
                    sh = next((s for s in es if s["file"] == req.file), None)
                lvl = (sh or {}).get("level", "")
                if sh and sh.get("building") and buildings(proj.get("sheets", [])):  # 'Primary School · Level 00'
                    lvl = f"{sh['building']} · {lvl or 'no level'}"
                item = upsert_item(proj, _item_from(req.question, req.answer, "sheet" if req.scope != "building" else
                                                    "building", req.file, req.page, (sh or {}).get("label", ""), lvl))
                save(pid, proj, visitor)
            return {"item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items/{iid}")  # POST: the web middleware forwards bodies of POSTs only
    @router.patch("/{pid}/items/{iid}")
    def patch_route(pid: str, iid: str, patch: dict, visitor: str = "") -> dict:
        try:
            with _LOCK:
                proj = _get(pid, visitor)
                item = patch_item(proj, iid, patch)
                save(pid, proj, visitor)
            return {"item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items/{iid}/delete")
    @router.delete("/{pid}/items/{iid}")
    def delete_item(pid: str, iid: str, visitor: str = "") -> dict:
        try:
            with _LOCK:
                proj = _get(pid, visitor)
                n = len(proj.get("items", []))
                proj["items"] = [i for i in proj.get("items", []) if i["id"] != iid]
                if len(proj["items"]) == n:
                    raise KeyError(iid)
                save(pid, proj, visitor)
            return {"project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/sheet")
    @router.patch("/{pid}/sheet")
    def patch_sheet(pid: str, patch: dict, visitor: str = "") -> dict:
        sid = str(patch.get("sheet") or "")
        try:
            with _LOCK:
                proj = _get(pid, visitor)
                sh = next((s for s in proj.get("sheets", []) if s["id"] == sid), None)
                if sh is None:
                    raise KeyError(sid)
                u = dict(sh.get("user") or {})
                for k in ("level", "role", "building"):
                    if k in patch:
                        v = str(patch[k] or "").strip()[:60]
                        if v:
                            u[k] = v
                        else:
                            u.pop(k, None)
                if "include" in patch:
                    u["include"] = None if patch["include"] is None else bool(patch["include"])
                sh["user"] = u
                save(pid, proj, visitor)
            mod = _f7()
            if mod is not None and (patch.get("level") or patch.get("role")):
                try:  # F7 'sheet': the user's name for the sheet shows on later answers
                    mod.add(sh["file"], "sheet", {"page": sh["page"], "storey": u.get("level"), "label": u.get("role"),
                                                  "title": sh.get("title")})
                except Exception:  # noqa: BLE001
                    pass
            return view(proj)
        except (ValueError, KeyError) as e:
            _err(e)

    @router.get("/{pid}/export")
    def export(pid: str, fmt: str = "csv", visitor: str = ""):
        try:
            proj = _get(pid, visitor)
        except (ValueError, KeyError) as e:
            _err(e)
        stem = re.sub(r"[^A-Za-z0-9]+", "-", proj.get("name") or "project").strip("-")[:40] or "project"
        if fmt == "xlsx":
            data = to_xlsx(proj)
            if data is not None:
                return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                headers={"Content-Disposition": f'attachment; filename="{stem}-review.xlsx"'})
        return Response(to_csv(proj), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{stem}-review.csv"'})
except ImportError:  # pragma: no cover - fastapi missing: the functions above still work
    router = None
