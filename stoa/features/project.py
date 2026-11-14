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

VERSION = 1
_FILE = ".stoa_project.json"
_LOCK = threading.Lock()
MAX_PAGES = 40  # pages classified per PDF (a 150-page set: the first 40; ~0.1 s per page)
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


def store_path(pid: str) -> Path:
    _check_pid(pid)
    if not pid.startswith("p"):
        d = uploads_root() / pid
        if d.is_dir():
            return d / _FILE
    return projects_dir() / f"{pid}.json"


def load(pid: str) -> dict | None:
    sp = store_path(pid)
    try:
        return json.loads(sp.read_text())
    except (OSError, ValueError):
        return None


def save(pid: str, data: dict) -> dict:
    sp = store_path(pid)
    sp.parent.mkdir(parents=True, exist_ok=True)
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
    r"\b(?:level|floor|storey|story|kerros|taso|etage|geschoss)\s*(?P<n2>-?\d{1,3})\b|"
    r"\b(?P<n3>\d{1,2})\.?\s*(?:kerros|krs|og)\b|\b(?P<roof>roof|mezzanine)\b|"
    r"\b(?P<ab>gf|lgf|ugf|ff|sf|tf|eg|ug|b[1-3]|l-?\d{1,2})\b", re.I)
_ORDS = "basement|ground|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\\d{1,2}(?:st|nd|rd|th)"
_AB = {"gf": 0, "ugf": 0, "lgf": -1, "ff": 1, "sf": 2, "tf": 3, "eg": 0, "ug": -1, "b1": -1, "b2": -2, "b3": -3}


def level_name(key: str) -> str:
    """A storey key as a neutral name: '0' -> 'Level 00', 'roof' -> 'Roof', '-1' -> 'Basement -1'."""
    if key in ("roof", "mezzanine"):
        return key.capitalize()
    try:
        n = int(float(key))
    except (TypeError, ValueError):
        return str(key)
    return f"Level {n:02d}" if n >= 0 else f"Basement {n}"


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
    if re.search(rf"\b(?:{_ORDS})\s+(?:and|&|\+|to)\s+(?:{_ORDS})\b", text, re.I):
        return None, None  # 'ground and first floor': one sheet, two levels
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
        elif m.group("n3") is not None:
            hits.append((str(int(m.group("n3"))), m.group(0).strip()))
        elif m.group("roof"):
            hits.append((m.group("roof").lower(), m.group("roof").capitalize()))
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


def classify_file(virtual: str, abs_path: str | None = None) -> list[dict]:
    """One uploaded file -> its sheets (one per PDF page, one per image) as plain dicts."""
    abs_path = abs_path or resolve(virtual)
    low = abs_path.lower()
    raw, media = [], {}
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
    for s in raw:
        number = s.number if s.number and not _PAPER.match(s.number) else None
        kind, kind_src = s.kind or "unknown", "title block"
        if kind in ("key_plan", "drawing") and one and role_from_name(virtual) == "site_plan":
            kind, kind_src = "site_plan", "file name"  # 'Primary_school_landscape_master_plan.pdf' read as a key plan
        if kind in ("unknown", "drawing"):
            guess = role_from_name(virtual) if one else None
            if guess:
                kind, kind_src = guess, "file name"
            elif kind == "unknown":
                kind_src = "not read"
        storey, level, level_src = s.storey, s.storey_name, "title block"
        if storey is None and one:
            storey, level = level_from_name(virtual)
            level_src = "file name" if storey is not None else ""
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
    if u.get("role"):
        inv = {v.lower(): k for k, v in ROLE_NAMES.items()}
        e["kind"] = inv.get(str(u["role"]).lower(), u["role"])
        e["role"], e["kind_source"] = ROLE_NAMES.get(e["kind"], str(u["role"])), "you"
    e["include"] = u.get("include")
    e["label"] = _label(e)
    return e


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
    find_copies(sheets, abs_of)
    users = {s["id"]: s.get("user") or {} for s in (old or {}).get("sheets", [])}
    for s in sheets:
        s["user"] = users.get(s["id"], {})
    old_name = (old or {}).get("name") if (old or {}).get("name_user") else ""
    proj = {"version": VERSION, "id": project_id(files), "name": name or old_name or _guess_name(sheets, files),
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


def find_copies(sheets: list[dict], abs_of: dict | None = None) -> None:
    """Mark a sheet that is another copy of an uploaded sheet: s['copy_of'] = the original's id, s['copy_how'] = why.
    In place; a copy without a level of its own takes the original's (level_source 'copy of …')."""
    order = {s["id"]: i for i, s in enumerate(sheets)}
    for s in sheets:
        for k in ("copy_of", "copy_how", "copy_score", "title_read"):
            s.pop(k, None)
    # (1) the same printed drawing number in two files
    keyed = [(s, _num_key(s.get("number"))) for s in sheets if _num_key(s.get("number"))]
    for i, (s, k) in enumerate(keyed):
        if s.get("copy_of"):
            continue
        group = [s] + [t for t, kt in keyed[i + 1:] if t["file"] != s["file"] and not t.get("copy_of") and _near(k, kt)]
        if len(group) < 2:
            continue
        group.sort(key=lambda g: (g.get("medium") != "vector", order[g["id"]]))
        orig = group[0]
        for g in group[1:]:
            if g["storey"] is not None and orig["storey"] is not None and g["storey"] != orig["storey"]:
                continue  # one number, two levels read: not a copy (a typo in a title block?)
            g["copy_of"], g["copy_how"] = orig["id"], f"the same drawing number, {orig['number']}"
    # (2) a picture / scanned page with the same level AND (OCR-tolerant) title as a CAD sheet
    import difflib

    def tnorm(t: str) -> str:  # OCR's usual confusions folded the same way on both sides (O/0, l/I/1, S/5)
        t = re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()
        return t.translate(str.maketrans({"o": "0", "l": "1", "i": "1", "s": "5"}))

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
    if scans and cands and abs_of:
        thumbs: dict = {}

        def th(s):
            if s["id"] not in thumbs:
                try:
                    thumbs[s["id"]] = _thumb(abs_of[s["id"]], s["page"])
                except Exception:  # noqa: BLE001 - an unreadable picture is simply not matched
                    thumbs[s["id"]] = None
            return thumbs[s["id"]]

        for sc in scans:
            t = th(sc)
            if t is None:
                continue
            scored = sorted(((_alike(t, th(c)), j, c) for j, c in enumerate(cands) if th(c) is not None),
                            key=lambda x: (-x[0], x[1]))
            if not scored:
                continue
            best, _, c = scored[0]
            second = scored[1][0] if len(scored) > 1 else 0.0
            if best < COPY_LOOK or best - second < COPY_MARGIN:
                continue
            if sc["storey"] is not None and c["storey"] is not None and sc["storey"] != c["storey"]:
                continue
            sc["copy_of"] = c["id"]
            sc["copy_how"] = f"the drawing is {best:.0%} alike"
            sc["copy_score"] = round(best, 3)
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


def _guess_name(sheets: list[dict], files: list[str]) -> str:
    """'Secondary School' from 'Secondary School GA Plan Level 00' / '... Level 01' (the words every title shares)."""
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
    for s in map(effective, sheets):
        key = (s.get("building") or "", s["storey"] if s["storey"] is not None else "?")
        r = rows.setdefault(key, {"storey": s["storey"], "level": s["level"] or "No level read", "building": s["building"],
                                  "sheets": []})
        r["sheets"].append(s["id"])
    return sorted(rows.values(), key=lambda r: (r["building"] or "", r["storey"] is None, order(r["storey"])))


def view(proj: dict) -> dict:
    """What the web shows: sheets with their effective labels, the level table, the review list, open questions."""
    sheets = [effective(s) for s in proj.get("sheets", [])]
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


def plan(proj: dict, question: str) -> dict:
    """The sheets a building-level question needs, and why the others are left out."""
    sheets = [effective(s) for s in proj.get("sheets", [])]
    chosen, why_not, reason, conf = [], {}, "", 0.6
    try:
        from stoa.sheet.sets import Sheet, pick_sheets

        objs = [Sheet(file=s["id"], page=0, number=s["number"] or None, title=s["title"] or s["role"],
                      kind=s["kind"] if s["kind"] not in ("unknown",) else "drawing", storey=s["storey"],
                      storey_name=s["level"] or None, building=s["building"] or None, partial=s["partial"])
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
        elif s["kind"] in NOT_SUMMED:
            why_not[s["id"]] = f"a {s['role'].lower()}: its quantities are not added to the building's"
        elif s["id"] in ids:
            chosen.append(s)
        elif s["partial"]:
            why_not[s["id"]] = f"an enlarged / partial plan of {s['level'] or 'a part of the building'} (its level has a whole plan)"
        else:
            why_not[s["id"]] = "not the kind of sheet this question is about"
    # a partial / enlarged plan of a level that also has a whole plan: counted once (the whole one)
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
    keep.sort(key=lambda s: (s["building"] or "", s["storey"] is None, _order(s["storey"]), s["file"], s["page"]))
    return {"sheets": [{"id": s["id"], "file": s["file"], "page": s["page"], "label": s["label"], "level": s["level"],
                        "role": s["role"]} for s in keep],
            "skipped": [{"id": s["id"], "label": s["label"], "why": why_not[s["id"]]} for s in sheets if s["id"] in why_not],
            "reason": reason, "confidence": conf, "sheet_question": sheet_question(question),
            "floor_area": is_floor_area_question(question)}


def level_in_question(question: str) -> str | None:
    """The storey key a question names ('on the first floor' -> '1', 'Level 02' -> '2', 'ground floor' -> '0',
    'basement' -> '-1', 'roof' -> 'roof'); None when it names none or several."""
    q = re.sub(r"\b(?:this|that|each|every|per|all|whole|entire)\s+(?:floors?|levels?|storeys?|stories)\b", " ", str(question or ""),
               flags=re.I)
    k, _ = level_from_name(re.sub(r"\s+", "_", q.strip()) + ".x")
    return k


def route(proj: dict, question: str, current: str = "") -> dict | None:
    """A question that names a level other than the sheet on screen ('How many doors are on the first floor?' while
    Level 00 is open) -> that level's whole floor plan, or None (ask the sheet on screen)."""
    k = level_in_question(question)
    if k is None:
        return None
    sheets = [effective(s) for s in proj.get("sheets", [])]
    cur = next((s for s in sheets if s["id"] == current), None) or next(
        (s for s in sheets if s["file"] == current.split("#")[0]), None)
    if cur is not None and cur["storey"] == k:
        return None
    cands = [s for s in sheets if s["storey"] == k and s["kind"] in ("floor_plan", "drawing") and s.get("include") is not False
             and (not s.get("copy_of") or s.get("include") is True)]
    whole = [s for s in cands if not s["partial"]] or cands
    if len(whole) != 1:
        return None  # none, or two plans of that level: the sheet on screen answers (and may ask which)
    s = whole[0]
    return {"id": s["id"], "file": s["file"], "page": s["page"], "label": s["label"], "level": s["level"]}


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


def combine(proj: dict, question: str, parts: list[dict]) -> dict:
    """Per-sheet answers -> a building answer (Answer-shaped dict) + the per-sheet table. No total when the sheets
    cannot be added safely; the reason is said instead (value None, 'per_sheet' still filled)."""
    sheets = {s["id"]: effective(s) for s in proj.get("sheets", [])}
    rows, problems = [], []
    for p in parts:
        sid = p.get("sheet") or f"{p.get('file')}#{int(p.get('page') or 0)}"
        s = sheets.get(sid)
        a = p.get("answer") or {}
        rows.append({"sheet": sid, "label": s["label"] if s else sid, "level": (s or {}).get("level", ""),
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
    if len(no_level) > 1:
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
    total, unit = None, None
    vertical = bool(_VERTICAL.search(question or "")) and counted and all(
        (_unit(r["unit"]) or ("?", 1))[0] == "count" for r, _ in counted)
    if vertical and not problems:
        # a lift on three plans is ONE lift: the most seen on one level, never the sum (2 + 2 + 1 lifts != 5)
        top_r, top_v = max(counted, key=lambda rv: rv[1])
        total, unit = int(top_v) if float(top_v).is_integer() else top_v, top_r["unit"]
        for r, _ in counted:
            r["counted"] = r is top_r
            r["note"] = "" if r is top_r else "not added: the same lifts / stairs pass through every level"
        each = ", ".join(f"{_fmt(v)} on {r['level'] or r['label']}" for r, v in counted)
        ev = [dict(e, note=f"{top_r['level'] or top_r['label']}: {e.get('note') or ''}".rstrip(": "))
              for e in top_r["evidence"]]
        return {"value": total, "unit": unit, "confidence": round(min(0.5, top_r["confidence"]), 3), "evidence": ev,
                "solver": "project/sheets", "scope": "building",
                "trace": [f"project: stairs / lifts / shafts / columns run through several levels: counts per level "
                          f"({each}) are not added; the building count is the most on one level = {_fmt(total)}"],
                "assumptions": [f"vertical elements appear on every level they serve: per level {each}; the building "
                                f"count is the largest ({_fmt(total)} on {top_r['level'] or top_r['label']}), assuming "
                                f"each one reaches that level"],
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
    if total is not None:
        trace = [f"project: building-level answer over {len(counted)} sheet(s): {each} = {_fmt(total)}"]
        assumptions = [f"one plan per level, summed: {each}"]
        skipped = [r for r in rows if not r["counted"] and r["note"]]
        assumptions += [f"{r['label']}: {r['note']}" for r in skipped]
        assumptions += [f"{r['label']}: no level read from it; counted as a level of its own (name its level if it "
                        f"repeats another)" for r, _ in counted if r["storey"] is None]
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
              "cost": _sum_cost([p.get("answer") or {} for p in parts]), "scope": "building",
              "per_sheet": [{k: v for k, v in r.items() if k != "evidence"} | {"n_evidence": len(r["evidence"])}
                            for r in rows],
              "no_total_why": "; ".join(problems) if total is None else ""}
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


def _ambiguity(answer: dict) -> dict | None:
    m = _SAME_LABEL.search(" ".join(str(a) for a in answer.get("assumptions") or []))
    if not m or answer.get("value") is None:
        return None
    choices = []
    for i, e in enumerate(answer.get("evidence") or []):
        mm = _EV_VALUE.match(str(e.get("note") or ""))
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
            "solver": answer.get("solver", ""), "evidence": ev, "n_evidence": len(answer.get("evidence") or []),
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
                it["f7"] = _to_f7(it)
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


def _to_f7(it: dict) -> str:
    """A corrected this-sheet value goes to F7's memory too (the same question on that file answers it, labelled as
    the user's); building totals stay in the project only. Returns the F7 item id or '' (fail closed)."""
    mod = _f7()
    if mod is None or it.get("scope") != "sheet" or not it.get("file"):
        return ""
    try:
        c = it["corrected"]
        got = mod.add(it["file"], "value", {"question": it["question"], "value": c["value"], "unit": c.get("unit"),
                                            "page": it.get("page"), "note": c.get("note") or "project review list"})
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


def floor_area(abs_path: str, virtual: str, page: int = 0, storey: str | None = None) -> dict:
    """A sheet's floor area (Answer-shaped): a PRINTED floor-area table first (its line for the sheet's own level),
    else the net sum of its traced rooms. value None when neither can be read (a picture, no printed scale, < half
    of the labels traced): then the caller asks the pipeline instead."""
    st = os.stat(abs_path)
    key = (abs_path, int(page), st.st_size, int(st.st_mtime), storey)
    with _FA_LOCK:
        if key in _FA_CACHE:
            return _FA_CACHE[key]
    t0 = time.time()
    out = {"value": None, "unit": "m2", "confidence": 0.0, "evidence": [], "solver": "project/rooms_sum",
           "assumptions": [], "missing": [], "trace": [], "fallback": True}
    if abs_path.lower().endswith(".pdf"):
        pr = printed_floor_areas(abs_path, page)
        if pr and storey is not None and storey in pr["levels"]:
            v, u, bb = pr["levels"][storey]
            out.update({"value": v, "unit": u, "confidence": 0.8, "solver": "project/printed_area", "fallback": False,
                        "evidence": [{"file": virtual, "page": int(page), "bbox": bb, "bbox_space": "pdf_pt",
                                      "note": f"printed {pr['kind']} for this level: {v:g} {u}"}],
                        "assumptions": [f"the {pr['kind']} printed on the sheet for this level ({v:g} {u})"
                                        + (f"; the sheet's printed total is {pr['total'][0]:g} {pr['total'][1]}"
                                           if pr.get("total") else "")],
                        "trace": [f"printed_area: {pr['kind']} table, level {storey} = {v:g} {u}"],
                        "printed_total": pr["total"][0] if pr.get("total") else None})
            with _FA_LOCK:
                _FA_CACHE[key] = out
            return out
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
    head = ["question", "level", "sheet", "value", "unit", "confidence", "added", "note"]
    body = []
    for it in proj.get("items", []):
        for r in it.get("per_sheet") or []:
            body.append([it["question"], r.get("level", ""), r.get("label", ""), "" if r.get("value") is None else r["value"],
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
def _open(files: list[str], name: str = "", refresh: bool = False) -> dict:
    files = [str(f) for f in files if str(f).strip()]
    if not files:
        raise ValueError("a project needs at least one file")
    pid = project_id(files)
    with _LOCK:
        old = load(pid)
        if old and not refresh and sorted(old.get("files", [])) == sorted(files) and old.get("sheets"):
            # re-read sheets nobody could read before (an image whose text layer has landed since)
            if any(s.get("kind_source") == "not read" or s.get("storey") is None for s in old["sheets"]):
                proj = build(files, name, old)
                return save(pid, proj)
            if name and name != old.get("name"):
                old["name"], old["name_user"] = name, True
                return save(pid, old)
            if not old.get("name_user"):  # a better automatic name since (copies left out of it)
                auto = _guess_name(old["sheets"], files)
                if auto != old.get("name"):
                    old["name"] = auto
                    return save(pid, old)
            return old
        return save(pid, build(files, name, old))


def _get(pid: str) -> dict:
    p = load(_check_pid(pid))
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

    class CombineIn(BaseModel):
        question: str
        parts: list[dict]

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
    def open_project(req: OpenIn) -> dict:
        try:
            return view(_open(req.files, req.name, req.refresh))
        except (ValueError, KeyError, FileNotFoundError) as e:
            _err(e)

    @router.get("/{pid}")
    def get_project(pid: str) -> dict:
        try:
            return view(_get(pid))
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/plan")
    def plan_route(pid: str, req: PlanIn) -> dict:
        try:
            return plan(_get(pid), req.question)
        except (ValueError, KeyError) as e:
            _err(e)

    class RouteIn(BaseModel):
        question: str
        current: str = ""

    @router.post("/{pid}/route")
    def route_route(pid: str, req: RouteIn) -> dict:
        try:
            return {"sheet": route(_get(pid), req.question, req.current)}
        except (ValueError, KeyError) as e:
            _err(e)

    class FloorIn(BaseModel):
        sheet: str

    @router.post("/{pid}/floor_area")
    def floor_area_route(pid: str, req: FloorIn) -> dict:
        """One sheet's net floor area as the sum of its traced rooms (Answer-shaped; value None + fallback = ask the
        pipeline instead)."""
        try:
            proj = _get(pid)
            sh = next((s for s in proj.get("sheets", []) if s["id"] == req.sheet), None)
            if sh is None:
                raise KeyError(req.sheet)
            e = effective(sh)
            return floor_area(resolve(sh["file"]), sh["file"], int(sh["page"]), e.get("storey"))
        except (ValueError, KeyError, FileNotFoundError) as e:
            _err(e)

    @router.post("/{pid}/combine")
    def combine_route(pid: str, req: CombineIn) -> dict:
        try:
            with _LOCK:
                proj = _get(pid)
                ans = combine(proj, req.question, req.parts)
                levels = ", ".join(r["level"] or r["label"] for r in ans["per_sheet"] if r["counted"])
                item = upsert_item(proj, _item_from(req.question, ans, "building", label=f"{len(req.parts)} sheets",
                                                    level=levels or "whole building"))
                save(pid, proj)
            return {"answer": ans, "item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items")
    def add_item(pid: str, req: ItemIn) -> dict:
        try:
            with _LOCK:
                proj = _get(pid)
                sid = f"{req.file}#{int(req.page)}"
                sh = next((effective(s) for s in proj.get("sheets", []) if s["id"] == sid), None)
                if sh is None:
                    sh = next((effective(s) for s in proj.get("sheets", []) if s["file"] == req.file), None)
                item = upsert_item(proj, _item_from(req.question, req.answer, "sheet" if req.scope != "building" else
                                                    "building", req.file, req.page, (sh or {}).get("label", ""),
                                                    (sh or {}).get("level", "")))
                save(pid, proj)
            return {"item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items/{iid}")  # POST: the web middleware forwards bodies of POSTs only
    @router.patch("/{pid}/items/{iid}")
    def patch_route(pid: str, iid: str, patch: dict) -> dict:
        try:
            with _LOCK:
                proj = _get(pid)
                item = patch_item(proj, iid, patch)
                save(pid, proj)
            return {"item": item, "project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/items/{iid}/delete")
    @router.delete("/{pid}/items/{iid}")
    def delete_item(pid: str, iid: str) -> dict:
        try:
            with _LOCK:
                proj = _get(pid)
                n = len(proj.get("items", []))
                proj["items"] = [i for i in proj.get("items", []) if i["id"] != iid]
                if len(proj["items"]) == n:
                    raise KeyError(iid)
                save(pid, proj)
            return {"project": view(proj)}
        except (ValueError, KeyError) as e:
            _err(e)

    @router.post("/{pid}/sheet")
    @router.patch("/{pid}/sheet")
    def patch_sheet(pid: str, patch: dict) -> dict:
        sid = str(patch.get("sheet") or "")
        try:
            with _LOCK:
                proj = _get(pid)
                sh = next((s for s in proj.get("sheets", []) if s["id"] == sid), None)
                if sh is None:
                    raise KeyError(sid)
                u = dict(sh.get("user") or {})
                for k in ("level", "role"):
                    if k in patch:
                        v = str(patch[k] or "").strip()[:60]
                        if v:
                            u[k] = v
                        else:
                            u.pop(k, None)
                if "include" in patch:
                    u["include"] = None if patch["include"] is None else bool(patch["include"])
                sh["user"] = u
                save(pid, proj)
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
    def export(pid: str, fmt: str = "csv"):
        try:
            proj = _get(pid)
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
