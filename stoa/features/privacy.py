"""Feature 'privacy' (T2, audit 19:51 fix #3): what is stored where and for how long, and 'Delete my files'.

    GET  /feat/privacy/note                 -> {"text": ..., "items": [{what, where, kept}], "private_mode": ...}
    POST /feat/privacy/delete {"upload": "upload/<id>/<name>" | "<id>"}
         -> {"ok", "deleted": [paths], "kept": [{where, why}]}

An uploaded file lives in $STOA_UPLOADS (default <tmp>/stoa-uploads, web/mock/stoaApi.ts) under a folder named by
its content (<id> = 12 hex). Format copies (PNG of a HEIC, PDF of a DXF, 6000-px copies) are written next to it,
so deleting the folder removes them. Readers also keep per-file caches elsewhere; this deletes the ones it can tie
to the upload: a cache file named after the uploaded file's name AND whose content names the upload folder (OCR,
image text layer, room-label readings, knowledge graph), the DXF plan render (key = path + mtime, recomputed), the
thumbnails, and the copies of the drawing named by a hash of path + size + time it recomputes: the page copies of a
multi-page PDF (stoa.pipeline), the PDF text reader's page renders (O1/pdfpng) and the door detector's page renders
(<tmp>/stoa_det), also for those page copies; F2's generated takeoffs (named by the file's content) and T3's
multi-upload project records once none of their uploads is left (F7's corrections and one-upload projects live in the
upload folder). tests/test_feat_privacy.py makes them with the owners' own functions, so
a change of a key scheme fails a test instead of leaving copies behind. Caches it cannot tie to the file (the model
response cache, keyed by a hash of the request; region crops / masked copies / room maps, keyed with the region asked
about; revision-compare images of a pair with a DATASET sheet) are listed under 'kept' with the reason, never
silently skipped. Revision-compare images of this upload against itself / another upload are found by recomputing
F5's pair key while the files still exist (cycle 4).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/feat/privacy", tags=["privacy"])
UPLOAD_ID = re.compile(r"^[0-9a-f]{12}$")


def uploads_root() -> Path:
    return Path(os.environ.get("STOA_UPLOADS") or Path(tempfile.gettempdir()) / "stoa-uploads")


def shared_root() -> Path:
    return Path(os.environ.get("JUNCTION_SHARED", ".stoa"))


def _named_cache_dirs() -> list[Path]:
    a = shared_root() / "artifacts"
    return [a / "O1" / "ocr", a / "W3" / "textlayer", a / "R1" / "room_labels", a / "W1" / "kg"]


NOTE = [
    {"what": "Your uploaded files and their format copies",
     "where": "this server's disk, a temporary folder named by the file's content and name (the very same file "
              "uploaded twice is stored once; our example sheets are shared by every visitor and are never deleted)",
     "kept": "until you press 'Delete my files' or the folder is cleared by the operator"},
    {"what": "Readings of your file (text found on the sheet, OCR, page renders, room labels, a building model's parsed "
             "index)",
     "where": "this server's disk caches, so a second question is fast",
     "kept": "'Delete my files' removes the ones tied to your file, page renders and page copies included; crops of "
             "a region you asked about and cached model answers are named by a hash, not by your file, and stay "
             "until the operator clears the caches"},
    # T2 cycle 4, from the code: vision.py sends the page + (counts on a large sheet) its 4 quarters zoomed in;
    # verify.py the page + a zoomed crop of the evidence; gemini.py (agent) a page thumbnail + our readers' text
    {"what": "Images of the page (the whole page; for a count on a large sheet also its four quarters zoomed in; for "
             "a second opinion a zoomed crop of the evidence), the question, the proposed answer and text our "
             "readers found on the sheet",
     "where": "Google Gemini (vision step / agent) or NVIDIA Nemotron (second opinion), ONLY when an answer is unsure "
              "and Private is off; each answer lists where its data went",
     "kept": "under the provider's API terms; never with Private on (no connection leaves this server then)"},
    {"what": "Your corrections, project groupings, takeoff lists and check-list ticks",
     "where": "your upload's folder, or a cache named by your file's content",
     "kept": "until you press 'Delete my files' (it removes them with the file)"},
    {"what": "Your questions and our answers",
     "where": "the server's memory (an answer cache, so the same question is answered at once); not written to disk",
     # T2 cycle 4: the web server forgets them on Delete (W5 1d8be27b = experiments/t2/w5_forget_deleted.patch, on
     # night f8973dfe); an older web server does not, and the delete result line says which
     "kept": "until you press 'Delete my files' (this server then forgets its answers about your file; the result "
             "says so) or the server restarts; no account, no tracking"},
    # cycle 8: the demo's access log (captain-api-8100 / 8102) also holds the browser's random id (?visitor= on the
    # plug-ins' GETs: corrections, project lists, check-list) and the file-search box text (/files?q=); never a question
    {"what": "Request log", "where": "the server's access log: request addresses, which include your file's name, this "
                                      "browser's random id (it keeps your corrections apart from other visitors') and "
                                      "what you type in the file search (not the drawing, not your questions)",
     "kept": "for the event"},
]


# T2 cycle 6: services this server's readers call on 127.0.0.1. A port of this machine can forward outside (an ssh
# tunnel; V1's det_server with DET_REMOTE_URL sends each page image to a GPU twin on a rented machine); netguard finds
# those ports (netguard.relays) and refuses them in private mode, and the note says so while one is configured.
_SERVICES = [
    ("STOA_DET_URL", "http://127.0.0.1:8140", "door / window detector", "images of the page"),
    ("STOA_OCR_URL", "http://127.0.0.1:8110", "text reader (OCR)", "images of the page"),
    ("LLM_BASE_URL", "http://127.0.0.1:8001/v1", "local model", "images of the page, the question and text read "
                                                                 "from the sheet"),
    ("LLM2_BASE_URL", "http://127.0.0.1:8002/v1", "second local model", "the question and text read from the sheet"),
]


def forwarding_services() -> list[dict]:
    """The services above whose URL is a port of this machine that forwards outside, right now."""
    try:
        from stoa import netguard

        relay_for_url = netguard.relay_for_url
    except (ImportError, AttributeError):
        return []
    out = []
    for env, default, name, what in _SERVICES:
        url = (os.environ.get(env) or default).strip()
        why = relay_for_url(url) if url else None
        if why:
            out.append({"service": name, "env": env, "why": why, "what": what})
    return out


def note() -> dict:
    priv = os.environ.get("STOA_PRIVATE", "").strip() == "1"
    text = ("Files you upload stay on this server (a temporary folder) and are read by our own readers and models "
            "running here. With Private off, an unsure answer may send images of the page, the question, the proposed "
            "answer and text our readers found on the sheet to Google Gemini or NVIDIA Nemotron; every answer says "
            "where its data went. With Private on, "
            "nothing leaves this server: the network is closed to outside addresses while it answers. "
            "'Delete my files' removes your upload and the readings tied to it.")
    items = [dict(i) for i in NOTE]
    fwd = forwarding_services()
    for f in fwd:
        items.insert(3, {"what": f"{f['what'][0].upper()}{f['what'][1:]} that our {f['service']} reads",
                         "where": f"a machine outside this server: the {f['service']} this server uses passes its "
                                  f"work on ({f['why']}); with Private on that connection is refused and the answer "
                                  "is made without it",
                         "kept": "by that machine for as long as it runs (the team's rented instance)"})
    if fwd:
        names = " and the ".join(f["service"] for f in fwd)
        text += (f" Right now the {names} this server uses passes its work to a machine outside this server; with "
                 "Private on it is not used.")
    raw = _saved_answer_minutes()
    if raw:
        # cycle 8: F7's corrections hook (night ffac20cd+, in the demo's feature subset) writes each answer it wraps to
        # <upload folder>/.stoa_raw/ (example sheets / dataset drawings: a shared folder) so a symbol correction fixes
        # the reading on screen; 'not written to disk' was no longer true there. Value, evidence and reasoning are in
        # the file, the question's wording is not (only a hash of it, in the file name).
        qa = next(i for i in items if i["what"] == "Your questions and our answers")
        qa["where"] = ("the server's memory (an answer cache, so the same question is answered at once); with the "
                       "corrections tool on, also the last answer to each question (its value, evidence and reasoning, "
                       "not your wording) on disk, in your upload's folder (for an example sheet a shared folder), so a "
                       "correction fixes the answer on screen without reading the drawing again")
        qa["kept"] = ("in memory: until you press 'Delete my files' (this server then forgets its answers about your "
                      "file; the result says so) or the server restarts. On disk: 'Delete my files' removes them with "
                      f"your folder; a saved answer older than {raw:g} minutes is also removed when the next one is "
                      "saved in the same folder (not on a timer; example sheets: only that way). No account, no tracking")
    ttl = ttl_hours()
    if ttl:
        text += f" Uploads are deleted automatically {ttl:g} hours after they arrive."
        items[0]["kept"] = (f"{ttl:g} hours after the upload (deleted automatically with its readings), or earlier "
                            "when you press 'Delete my files'")
    return {"text": text, "items": items, "private_mode_server_default": priv, "retention_hours": ttl or None,
            "measured": measures_destinations(), "forwarding": fwd}


def _saved_answer_minutes() -> float:
    """Minutes F7's corrections hook keeps an answer on disk (corrections.RAW_TTL, seconds), 0 when this tree's
    corrections module does not save answers (d32 and older) or cannot be imported."""
    try:
        from stoa.features import corrections

        if not callable(getattr(corrections, "_raw_put", None)):
            return 0.0
        return round(float(getattr(corrections, "RAW_TTL", 0) or 0) / 60.0, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def measures_destinations(ask_upload: Path | None = None) -> bool:
    """True when this server's upload answers say which outside hosts they reached, measured at the socket: netguard
    is installed here and this tree's web/server/ask_upload.py wraps each answer in netguard.watch() (night-W5
    8205a62a). The panel then shows 'no outside connection during this answer' when no host was reached."""
    try:
        from stoa import netguard

        f = ask_upload or Path(__file__).resolve().parents[2] / "web" / "server" / "ask_upload.py"
        src = f.read_text(errors="replace")
    except Exception:  # noqa: BLE001
        return False
    return bool(netguard._installed) and "netguard" in src and ".watch()" in src


# -- retention (cycle 2): opt-in automatic expiry ------------------------------------------------------------------
# STOA_UPLOAD_TTL_HOURS=<h> (unset / 0 = off, the default: the demo's sample uploads must not vanish). An upload
# folder older than <h> hours (newest file time in it) is removed with delete_upload(), i.e. with its readings; a
# folder holding a file named '.keep' (operator: demo samples) is never expired. Swept every STOA_UPLOAD_SWEEP_MIN
# minutes (default 15) by one daemon thread per API process, started when this router is mounted.

def ttl_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("STOA_UPLOAD_TTL_HOURS", "0") or 0))
    except ValueError:
        return 0.0


def _newest(folder: Path) -> float:
    t = folder.stat().st_mtime
    for p in folder.rglob("*"):
        try:
            t = max(t, p.stat().st_mtime)
        except OSError:
            pass
    return t


def expire(ttl: float | None = None, now: float | None = None) -> list[dict]:
    """Delete every upload folder older than ttl hours (default: STOA_UPLOAD_TTL_HOURS); -> one result per folder."""
    import time

    ttl = ttl_hours() if ttl is None else ttl
    root = uploads_root()
    if not ttl or not root.is_dir():
        return []
    now = time.time() if now is None else now
    out = []
    for d in sorted(root.iterdir()):
        try:
            if not (d.is_dir() and UPLOAD_ID.match(d.name)) or sample_reason(d):  # '.keep' / example folders stay
                continue
            if now - _newest(d) < ttl * 3600:
                continue
            out.append({"upload": d.name, **delete_upload(d.name)})
        except (OSError, ValueError):
            continue
    return out


def _sweeper() -> None:
    import sys
    import time

    every = max(1.0, float(os.environ.get("STOA_UPLOAD_SWEEP_MIN", "15") or 15)) * 60
    while True:
        try:
            gone = expire()
            if gone:
                print(f"[privacy] retention: {len(gone)} upload(s) older than {ttl_hours():g} h deleted",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - never take the API down
            print(f"[privacy] retention sweep failed: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(every)


def start_sweeper() -> bool:
    """One sweeper thread per process, only when STOA_UPLOAD_TTL_HOURS is set; True when it runs."""
    import threading

    global _SWEEPING
    if _SWEEPING or not ttl_hours():
        return _SWEEPING
    _SWEEPING = True
    threading.Thread(target=_sweeper, name="privacy-retention", daemon=True).start()
    return True


_SWEEPING = False


@router.get("/note")
def get_note() -> dict:
    return note()


class DeleteReq(BaseModel):
    upload: str
    # cycle 5: the browser's visitor ids (F7 / F6 localStorage), so F6's check-list ticks named by them can be found
    visitors: list[str] = []


def _upload_dir(ref: str) -> Path:
    """'upload/<id>/<name>' or '<id>' -> the folder; anything else (other paths, '..') is refused."""
    parts = [p for p in str(ref or "").strip().split("/") if p]
    if any(p in (".", "..") or "\\" in p for p in parts) or str(ref or "").startswith("/"):
        raise ValueError("not an upload reference")
    if parts and parts[0] == "upload":
        parts = parts[1:]
    if not parts or not UPLOAD_ID.match(parts[0]):
        raise ValueError("not an upload reference (expected upload/<12 hex id>/<name>)")
    root = uploads_root().resolve()
    d = (root / parts[0]).resolve()
    if d.parent != root:
        raise ValueError("not an upload folder")
    return d


def _mentions(path: Path, needles: list[str]) -> bool:
    try:
        if path.stat().st_size > 32 << 20:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    return any(n.encode() in data for n in needles)


def derived(folder: Path, visitors: list[str] | None = None) -> tuple[list[Path], list[dict]]:
    """Cache files tied to the upload in `folder` (deleted with it) and the caches that cannot be tied (kept).
    `visitors`: the browser ids the person's check-list ticks are filed under ('anon' always)."""
    files = [p for p in folder.rglob("*") if p.is_file()] if folder.is_dir() else []
    uid = folder.name
    needles = [str(folder), uid]
    out: list[Path] = []
    for d in _named_cache_dirs():
        if not d.is_dir():
            continue
        for f in files:
            stem = f.stem[:60]
            for c in d.glob(f"{glob_escape(stem)}*"):
                if c.is_file() and _mentions(c, needles):
                    out.append(c)
        out += [c for c in d.glob(f"*{uid}*") if c.is_file()]
    tmp = Path(tempfile.gettempdir())
    for f in files:
        try:
            key = hashlib.sha1(f"{f}|{os.path.getmtime(f)}".encode()).hexdigest()[:12]
        except OSError:
            continue
        out += [c for c in (Path(os.environ.get("STOA_CACHE", tmp)) / "stoa-dxfplan").glob(f"{glob_escape(f.stem)}.{key}.*")]
    out += [c for c in (tmp / "stoa-thumbs").glob(f"*{uid}*") if c.is_file()]
    # copies of the drawing itself, named by a hash this module can recompute (cycle 2): the one-page copies of a
    # multi-page upload (pipeline, a folder per file) and, for the upload AND those copies, the page renders of the
    # PDF text reader and of the door detector
    sources = list(files)
    for f in files:
        for key in _path_keys(f):
            pages = _page_dir() / key
            if pages.is_dir():
                out.append(pages)
                sources += [p for p in pages.rglob("*") if p.is_file()]
    for f in sources:
        for key in _path_keys(f):
            out += [c for c in (tmp / "stoa_det").glob(f"{key}.*.png") if c.is_file()]
        out += [c for c in (shared_root() / "artifacts" / "O1" / "pdfpng" / f"{k}.png" for k in _render_keys(f))
                if c.is_file()]
    # feature plug-ins (cycle 2): F2's generated takeoffs (a folder per sheet page, named by the file's content
    # sha256), T3's projects that group several uploads (a record in <tmp>/stoa-projects, removed once none of the
    # uploads it names is left). F7's corrections and T3's one-upload projects live in the upload folder itself.
    takeoff = Path(os.environ.get("STOA_TAKEOFF_CACHE") or tmp / "stoa-takeoff")
    shared: list[Path] = []  # content-keyed caches another upload of the same bytes still uses (cycle 3)
    for f in files:
        h = _sha256_16(f)
        found = [c for c in takeoff.glob(f"{h}-p*") if c.is_dir()] if h else []
        if found and _same_bytes_elsewhere(folder, f):
            shared += found
            out += _takeoff_fix_files(found, visitors)  # cycle 9: this browser's Measure fixes in the shared job
        else:
            out += found
    # cycle 9: copies of the drawing made by d33's readers outside the upload folder (named by its path)
    out += _upright_copies(_upright_keys(sources))
    out += _formats_side_dirs(sources)
    projects = Path(os.environ.get("STOA_PROJECTS_DIR") or tmp / "stoa-projects")
    for rec in projects.glob("p*.json") if projects.is_dir() else []:
        try:
            ids = set(re.findall(r"upload/([0-9a-f]{12})/", rec.read_text(errors="replace")))
        except OSError:
            continue
        if uid in ids and all(i == uid or not (folder.parent / i).is_dir() for i in ids):
            out.append(rec)
    # CC5K room maps: content-named once experiments/t2/r1_room_map_names.patch is applied (graph.map_name); before
    # that they are named by the file name alone, shared by every upload of that name, and are kept (listed below)
    try:
        from stoa.extras.room_graph.graph import map_name
    except Exception:  # noqa: BLE001 - no room-graph module / no cv2 here, or not patched yet
        map_name = None
    if map_name is not None:
        live = shared_root() / "artifacts" / "R1" / "cc5k_seg_live"
        for f in files:
            try:
                name = map_name(str(f))
            except Exception:  # noqa: BLE001
                continue
            if name != f.stem:
                found = [c for c in live.glob(f"{glob_escape(name)}.npz") if c.is_file()]
                if found and _same_bytes_elsewhere(folder, f):
                    shared += found
                else:
                    out += found
    kept = [{"where": str(shared_root() / "artifacts" / "llm-cache"),
             "why": "model responses are keyed by a hash of the request (image + question); they cannot be traced to "
                    "this file without the request; they hold the models' replies (which can repeat text read from "
                    "the sheet), never the drawing, the page images or the question"},
            {"where": ", ".join(str(p) for p in (shared_root() / "artifacts" / "VP" / "crops", tmp / "stoa-region",
                                                 shared_root() / "cache" / "det")),
             "why": "crops of a region of the page and region-masked copies are named by a hash that includes the "
                    "region / box asked about, which is not stored; they are not removed"}]
    # cycle 5: an uploaded building model's parsed index + parts map (W1), named by file name + size + mtime: the same
    # file uploaded twice lands in the same folder, so no other upload shares these
    out += _ifc_readings(folder, files)
    out += _checklist_ticks(folder, files, visitors or [])
    rc_dirs = _revcompare_dirs(folder)
    out += rc_dirs
    if (_revcompare_root()).is_dir():
        kept.append({"where": str(_revcompare_root()),
                     "why": "revision-compare images of this file against a DATASET sheet are not traced (against "
                            "another upload, or within your set, they are removed)"})
    ck_dir = Path(os.environ.get("STOA_CHECKLIST_DIR") or tmp / "stoa-checklist")
    if ck_dir.is_dir():
        kept.append({"where": str(ck_dir),
                     "why": "check-list ticks made in another browser are filed under that browser's id and are not "
                            "traced (this browser's are removed)"})
    if shared:
        kept.append({"where": ", ".join(str(p) for p in sorted(set(shared))),
                     "why": "the same file is also open as another upload (another visitor's copy or an example "
                            "sheet): the takeoff / room map read from its content stays for that upload"})
    if map_name is None:
        kept.append({"where": str(shared_root() / "artifacts" / "R1" / "cc5k_seg_live"),
                     "why": "room maps of pictures are named by the file name alone (shared by uploads of the same "
                            "name); not removed"})
    return sorted(set(out)), kept


def _ifc_dirs() -> tuple[Path, Path]:
    """W1's IFC readings (stoa/solvers/ifc/index.py): the whole model's parsed index (storeys, spaces, elements with
    names, quantities and geometry) pickled in artifacts/W1/ifc_index/, and its parts map in artifacts/W1/ifc_parts/."""
    w1 = shared_root() / "artifacts" / "W1"
    return w1 / "ifc_index", w1 / "ifc_parts"


def _ifc_versions() -> list[int]:
    """Every INDEX_VERSION W1's key may carry: 1 .. the one in stoa/solvers/ifc/index.py now (read from its source;
    pickles made under older versions stay on disk). Unreadable source: 1 .. 30."""
    try:
        src = (Path(__file__).resolve().parents[1] / "solvers" / "ifc" / "index.py").read_text(encoding="utf-8")
        m = re.search(r"^INDEX_VERSION\s*=\s*(\d+)", src, re.M)
        top = int(m.group(1)) if m else 30
    except OSError:
        top = 30
    return list(range(1, max(1, min(top, 200)) + 1))


def _ifc_names(f: Path) -> list[str]:
    """T2 cycle 5: the cache names W1 gives this model's readings while the file exists: '<stem>-<h>' with h =
    sha1('<name>|<size>|<int mtime>|v<V>')[:10] for every version V (index.py _cache_key); the index is '<it>.pkl'
    (written via '<it>.tmp<pid>'), the parts map '<it>.json'. Only .ifc files."""
    if f.suffix.lower() != ".ifc":
        return []
    try:
        st = f.stat()
    except OSError:
        return []
    return [f"{f.stem}-" + hashlib.sha1(f"{f.name}|{st.st_size}|{int(st.st_mtime)}|v{v}".encode()).hexdigest()[:10]
            for v in _ifc_versions()]


def _ifc_readings(folder: Path, files: list[Path], names: list[str] | None = None) -> list[Path]:
    """The IFC readings of the folder's models: exact names (from _ifc_names, or `names` recorded earlier) and, for a
    reading made when the file had another mtime (uploaded again), an index named after the file that mentions the
    folder (the pickle stores the model's path). A parts map holds GlobalIds only, so it is matched by exact name."""
    idx_dir, parts_dir = _ifc_dirs()
    names = [n for f in files for n in _ifc_names(f)] if names is None else names
    out = [c for n in names for c in (idx_dir / f"{n}.pkl", parts_dir / f"{n}.json") if c.is_file()]
    out += [c for n in names for c in idx_dir.glob(f"{glob_escape(n)}.tmp*") if c.is_file()] if idx_dir.is_dir() else []
    needles = [str(folder), folder.name]
    for stem in {f.stem for f in files if f.suffix.lower() == ".ifc"}:
        if idx_dir.is_dir():
            for c in idx_dir.glob(f"{glob_escape(stem)}-*.pkl"):
                if c.is_file() and c not in out and _mentions(c, needles):
                    out.append(c)
                    j = parts_dir / f"{c.stem}.json"
                    if j.is_file():
                        out.append(j)
    return out


VISITOR = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # F6's rule for a visitor id (checklist._ck_path)


def _checklist_ticks(folder: Path, files: list[Path], visitors: list[str]) -> list[Path]:
    """T2 cycle 5: F6's check-list ticks of this upload's sheets, one file per sheet page + visitor named by F6's own
    checklist._ck_path(virtual path, page, visitor) = <STOA_CHECKLIST_DIR or <tmp>/stoa-checklist>/<sha1>[:20].json.
    The ticks hold item ids and times only (no path), so they are found by name: for 'anon' and the visitor ids the
    browser sends (unknown ids cannot be traced; listed as kept)."""
    d = Path(os.environ.get("STOA_CHECKLIST_DIR") or Path(tempfile.gettempdir()) / "stoa-checklist")
    if not d.is_dir():
        return []
    try:
        from stoa.features.checklist import _ck_path
    except Exception:  # noqa: BLE001 - no check-list module in this build
        return []
    vs = ["anon", *dict.fromkeys(v for v in visitors[:8] if isinstance(v, str) and VISITOR.match(v))]
    out: list[Path] = []
    for f in files:
        if f.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg", ".dxf", ".svg", ".ifc", ".webp", ".tif", ".tiff"):
            continue
        virtual = "upload/" + f.relative_to(folder.parent).as_posix()
        pages = (_rc_variants(f) or ("", 0, 0, [0]))[3]
        for v in vs:
            for pg in pages:
                try:
                    c = _ck_path(virtual, pg, v)
                except Exception:  # noqa: BLE001
                    continue
                if c.is_file():
                    out.append(c)
    return out


# cycle 5: pure caches (rebuilt on demand) that the API process keeps per file: (module, attribute)
_MEMORY_CACHES = (("stoa.solvers.ifc.index", "_MEM"), ("stoa.features.xref", "_CACHE"),
                  ("stoa.features.revcompare", "_FULL"), ("stoa.features.corrections", "_REC"),
                  ("stoa.features.corrections", "_CROSS"), ("stoa.features.project", "_TXT"),
                  ("stoa.features.project", "_FA_CACHE"), ("stoa.features.takeoff", "_SUMMARY"),
                  ("stoa.features.trust_map", "_CACHE"))


def _memory_plan(folder: Path, files: list[Path]) -> dict:
    """What to forget in this process, fixed while the files exist: path needles (keys that name a file of the folder)
    and the trust map's content keys (trust_map._file_key for every page x OCR mode; its keys start with one)."""
    import sys

    needles = sorted({str(folder).rstrip("/") + "/", os.path.realpath(folder).rstrip("/") + "/", f"upload/{folder.name}/"})
    tm = sys.modules.get("stoa.features.trust_map")
    keys: set[str] = set()
    if tm is not None and getattr(tm, "_CACHE", None):
        for f in files:
            if f.suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg"):
                continue
            for pg in (_rc_variants(f) or ("", 0, 0, [0]))[3]:
                for ocr in ("auto", "off", "force"):
                    try:
                        keys.add(tm._file_key(str(f), pg, ocr))
                    except Exception:  # noqa: BLE001
                        pass
    return {"needles": needles, "trust_keys": sorted(keys)}


def _forget_in_memory(plan: dict) -> int:
    """Drop this process's cached readings of the upload (W1's parsed IFC models, xref sheet indexes, revision-compare
    page images, corrections, project text, takeoff summaries, trust maps). Only modules already loaded; other processes
    (the web server's answering workers) keep theirs until they restart."""
    import sys

    n = 0
    tkeys = tuple(plan.get("trust_keys") or ())
    for mod, attr in _MEMORY_CACHES:
        d = getattr(sys.modules.get(mod), attr, None)
        if not isinstance(d, dict):
            continue
        try:
            keys = list(d)
        except RuntimeError:  # resized by another thread right now: skip this one
            continue
        for k in keys:
            s = str(k)
            if any(x in s for x in plan["needles"]) or (tkeys and attr == "_CACHE" and mod.endswith("trust_map")
                                                       and s.startswith(tkeys)):
                if d.pop(k, None) is not None:
                    n += 1
    return n


def _revcompare_root() -> Path:
    """F5's revision-compare images: <STOA_FEAT_CACHE or <tmp>/stoa-feat>/revcompare/<key>/{a,b_aligned,overlay}.png."""
    return Path(os.environ.get("STOA_FEAT_CACHE", Path(tempfile.gettempdir()) / "stoa-feat")) / "revcompare"


_RC_MAX_PAGES = 30


def _rc_versions() -> list[int]:
    """Every version tag F5's key may carry: 1 .. the one in stoa/features/revcompare.py now (read from its source, the
    key's last element; F5 bumps it when the comparison changes, and folders made under older tags stay on disk).
    Unreadable source: 1 .. 30."""
    try:
        src = (Path(__file__).with_name("revcompare.py")).read_text(encoding="utf-8")
        m = re.search(r"st_b\.st_size,\s*page_b,\s*(\d+)\s*\]", src)
        top = int(m.group(1)) if m else 30
    except OSError:
        top = 30
    return list(range(1, max(1, min(top, 200)) + 1))


def _rc_variants(f: Path) -> tuple[str, int, int, list[int]] | None:
    try:
        st = f.stat()
    except OSError:
        return None
    pages = [0]
    if f.suffix.lower() == ".pdf":
        try:
            import pymupdf

            with pymupdf.open(f) as doc:
                pages = list(range(min(doc.page_count, _RC_MAX_PAGES))) or [0]
        except Exception:  # noqa: BLE001 — unreadable: page 0 only
            pass
    return str(f.resolve()), st.st_mtime_ns, st.st_size, pages


def _upright_keys(files: list[Path]) -> list[str]:
    """T2 cycle 9: F5's revision compare (d33, revcompare._upright_copy) re-plots a TURNED PDF page upright into
    <feat cache>/revcompare/upright/<key>.pdf, key = sha256([path, mtime_ns, size, page, 1])[:16]: a full vector copy
    of the uploaded page, named by THIS upload's path (no other upload shares it). Seen live: upload 23ba2beeccb8
    (school-first-floor-reissued-upside-down.pdf) -> upright/675d03da7e743d41.pdf, which Delete left behind. The file
    names are computed while the files exist (the late sweep needs them after). Every path spelling, version 1..3."""
    keys: list[str] = []
    for f in files:
        if f.suffix.lower() != ".pdf":
            continue
        v = _rc_variants(f)
        if v is None:
            continue
        _, mt, size, pages = v
        for sp in {str(f), os.path.abspath(f), str(f.resolve())}:
            for page in pages:
                for ver in (1, 2, 3):
                    keys.append(hashlib.sha256(json.dumps([sp, mt, size, page, ver]).encode()).hexdigest()[:16])
    return sorted(set(keys))


def _upright_copies(keys: list[str]) -> list[Path]:
    d = _revcompare_root() / "upright"
    return [c for c in (d / f"{k}.pdf" for k in keys) if c.is_file()] if d.is_dir() else []


def _formats_side_dirs(files: list[Path]) -> list[Path]:
    """T2 cycle 9: P1's readable_picture (d33, stoa.formats.normalize._side_cache; also _cache_dir when a file's folder
    is read-only) reads TIFF / WebP / BMP / GIF, 16-bit grey and transparent pictures on the CLI / API path from an
    8-bit PNG copy in $STOA_CACHE/formats/<sha1(resolved path)[:12]>/ (default ~/.cache/stoa): a full copy of the
    picture, named by this upload's path. Computed here, never created (normalize's helper would mkdir)."""
    root = Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "formats"
    if not root.is_dir():
        return []
    out = []
    for f in files:
        try:
            d = root / hashlib.sha1(str(f.resolve()).encode()).hexdigest()[:12]
        except OSError:
            continue
        if d.is_dir():
            out.append(d)
    return out


def _takeoff_fix_files(dirs: list[Path], visitors: list[str] | None) -> list[Path]:
    """T2 cycle 9: F2 files each browser's Measure fixes INSIDE the content-keyed takeoff job folder
    (takeoff._fixes_path(dir, visitor) = one file per visitor). When the folder itself stays (an example sheet, or the
    same bytes open as another upload), this browser's fix files in it still go with 'Delete my files'."""
    vis = {str(v).strip() for v in visitors or [] if str(v or "").strip() and str(v).strip() != "anon"}
    if not vis or not dirs:
        return []
    try:
        from stoa.features import takeoff

        fp = takeoff._fixes_path
    except Exception:  # noqa: BLE001 - no takeoff plug-in / an older one without per-visitor fixes
        return []
    out = []
    for d in dirs:
        for v in vis:
            try:
                f = fp(d, v)
            except Exception:  # noqa: BLE001 - a bad id
                continue
            if f.parent == d and f.name != "fixes-anon.json" and f.is_file():
                out.append(f)
    return out


def _revcompare_dirs(folder: Path) -> list[Path]:
    """T2 cycle 4: the revision-compare image folders of a comparison that involved a file of this upload. F5 names
    them sha256([abs A, mtime_ns A, size A, page A, abs B, mtime_ns B, size B, page B (None = best match), v])[:16]
    (revcompare.cached_compare), so they are found by recomputing that key for every pair of this upload's files with
    each other and with every other upload's files, both orders, every page (A: 0..n-1; B: None, 0..n-1), while the
    files still exist. Comparisons with a dataset sheet are not enumerated (the note says so)."""
    root = _revcompare_root()
    try:
        existing = {d.name for d in root.iterdir() if d.is_dir() and re.fullmatch(r"[0-9a-f]{16}", d.name)}
    except OSError:
        return []
    if not existing:
        return []
    mine = [v for f in sorted(folder.rglob("*")) if f.is_file() and (v := _rc_variants(f))]
    if not mine:
        return []
    up = uploads_root()
    others = [v for f in sorted(up.glob("*/*")) + sorted(up.glob("*/data/*/*"))
              if f.is_file() and folder not in f.parents and (v := _rc_variants(f))]
    found: set[str] = set()
    versions = _rc_versions()

    def key(a, pa, b, pb, v) -> str:
        return hashlib.sha256(json.dumps([a[0], a[1], a[2], pa, b[0], b[1], b[2], pb, v]).encode()).hexdigest()[:16]

    for a in mine:
        for b in mine + others:
            if b is a:
                continue
            for x, y in ((a, b), (b, a)):
                for pa in x[3]:
                    for pb in [None, *y[3]]:
                        for v in versions:
                            k = key(x, pa, y, pb, v)
                            if k in existing:
                                found.add(k)
    return [root / k for k in sorted(found)]


def _forget_compare_results(keys: list[str]) -> int:
    """Drop F5's in-memory compare results of those keys (same API process; revcompare._RESULTS / _LOCK)."""
    import sys

    rc = sys.modules.get("stoa.features.revcompare")
    res, lock = getattr(rc, "_RESULTS", None), getattr(rc, "_LOCK", None)
    if res is None or lock is None:
        return 0
    with lock:
        return sum(res.pop(k, None) is not None for k in keys)


def _sha256_16(f: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _page_dir() -> Path:
    return Path(os.environ.get("STOA_PAGE_DIR") or Path(tempfile.gettempdir()) / "stoa-pages")


def _path_keys(f: Path) -> list[str]:
    """sha1('<path>|<size>|<mtime_ns>')[:16]: how stoa.pipeline names a multi-page PDF's page-copy folder and
    stoa.solvers.door_detect its page renders (path as given or absolute: both spellings)."""
    try:
        st = f.stat()
    except OSError:
        return []
    return sorted({hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
                   for p in (str(f), os.path.abspath(f), str(f.resolve()))})


def _render_keys(f: Path) -> list[str]:
    """The names of stoa.solvers.vector.pdf_textqa.render_with_text's page renders of this PDF (every page)."""
    if f.suffix.lower() != ".pdf":
        return []
    try:
        import pymupdf

        from stoa.solvers.vector.pdf_textqa import DPI, MAX_SIDE

        mt = os.path.getmtime(f)
        with pymupdf.open(str(f)) as doc:
            sizes = [(pg.rect.width, pg.rect.height) for pg in doc]
    except Exception:  # noqa: BLE001 — not a readable PDF: nothing was rendered from it
        return []
    keys = set()
    for i, (w, h) in enumerate(sizes):
        dpi = max(36, int(min(DPI, MAX_SIDE / max(w, h, 1) * 72)))
        for p in (str(f), os.path.abspath(f), str(f.resolve())):
            keys.add(hashlib.sha1(f"{p}|{mt}|{i}|{dpi:.2f}".encode()).hexdigest()[:16])
    return sorted(keys)


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", s)


# -- shared uploads (cycle 3) ------------------------------------------------------------------------------------
# Upload folders are named by content + file name, so the catalogue's example sheets (W5 /api/case, W6 /api/m/case:
# opened as uploads from the example folders below and cached in the web server's memory), F5's revision demo and a
# visitor who uploads the very same file all share ONE folder. 'Delete my files' must not delete those (every later
# visitor of that catalogue card would get a missing file until the web server restarts), nor a cache keyed by
# content (F2's takeoff that W5's 'at a glance' card reads) while another upload still holds the same bytes.

def _sample_dirs() -> list[Path]:
    s = shared_root()
    dirs = [os.environ.get("STOA_EXAMPLES_DIR", ""), s / "artifacts" / "stoa-examples",
            s / "datasets" / "stoa-like" / "originals", s / "artifacts" / "W6" / "cases", s / "datasets" / "stoa-like" / "dfe",
            os.environ.get("STOA_F5_DEMO_DIR") or s / "artifacts" / "F5" / "demo"]
    dirs += [d for d in os.environ.get("STOA_SAMPLE_DIRS", "").split(os.pathsep) if d]
    return [Path(d) for d in dirs if str(d)]


_SAMPLES: dict = {"sig": None, "index": {}, "ids": {}}


def _image_norm_v() -> str:
    """web/mock/stoaApi.ts IMAGE_NORM_V (part of an image upload's folder id), read from this tree."""
    try:
        src = (Path(__file__).resolve().parents[2] / "web" / "mock" / "stoaApi.ts").read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r'IMAGE_NORM_V\s*=\s*"([^"]*)"', src)
    return m.group(1) if m else ""


def _safe_name(name: str) -> str:
    """stoaApi.ts saveUpload's file name rule (JS \\w is ASCII)."""
    safe = re.sub(r"[^\w.\- ()]+", "_", os.path.basename(name), flags=re.ASCII).lstrip(".")[-120:]
    return safe or "upload"


def upload_ids(body: bytes, name: str) -> set[str]:
    """The folder ids saveUpload gives these bytes under this name: sha256(body [+ IMAGE_NORM_V] + '|name:' + safe)
    [:12] (images get IMAGE_NORM_V; both are returned, the extension rule lives in the TS file)."""
    tail = f"|name:{_safe_name(name)}".encode()
    return {hashlib.sha256(body + v.encode() + tail).hexdigest()[:12] for v in ("", _image_norm_v())}


def _sample_index() -> dict[int, dict[str, set[str]]]:
    """size -> {sha256[:16] -> example file names} for the files at the top of the example folders, and in
    _SAMPLES['ids'] the upload folder id each example gets when the catalogue opens it (images are normalised IN PLACE
    on upload, so their folder holds other bytes than the example: the id is what identifies it). Re-read when a
    folder changes."""
    dirs = [d for d in _sample_dirs() if d.is_dir()]
    sig = tuple((str(d), d.stat().st_mtime_ns) for d in dirs) + (_image_norm_v(),)
    if _SAMPLES["sig"] != sig:
        index: dict[int, dict[str, set[str]]] = {}
        ids: dict[str, str] = {}
        for d in dirs:
            for f in d.iterdir():
                try:
                    if f.is_file() and not f.name.startswith("."):
                        index.setdefault(f.stat().st_size, {}).setdefault(_sha256_16(f), set()).add(f.name)
                        for i in upload_ids(f.read_bytes(), f.name):
                            ids[i] = f.name
                except OSError:
                    continue
        _SAMPLES.update(sig=sig, index=index, ids=ids)
    return _SAMPLES["index"]


def sample_reason(folder: Path) -> str | None:
    """Why this upload folder is shared and must stay, or None (an ordinary visitor upload). An example's folder =
    the example's bytes under the example's own name (that is the folder the catalogue opens; the same bytes under
    another name are a visitor's own folder, deleted as usual: the file name is theirs)."""
    if (folder / ".keep").exists():
        return "kept by the operator (a demo sample)"
    index = _sample_index()
    if folder.name in _SAMPLES["ids"]:  # the catalogue's own folder for an example (an image's bytes changed in place)
        return (f"it is one of our example sheets ({_SAMPLES['ids'][folder.name]}), opened by every visitor who picks "
                "it; nothing you uploaded is stored with it")
    for f in folder.iterdir() if folder.is_dir() else []:
        try:
            if f.is_file() and f.stat().st_size in index and f.name in index[f.stat().st_size].get(_sha256_16(f), ()):
                return (f"it is one of our example sheets ({f.name}), opened by every visitor who picks it; "
                        "nothing you uploaded is stored with it")
        except OSError:
            continue
    return None


def _same_bytes_elsewhere(folder: Path, f: Path) -> bool:
    """Another upload folder (another visitor, a catalogue example) holds a file with f's exact bytes."""
    try:
        size, h = f.stat().st_size, None
    except OSError:
        return False
    root = folder.parent
    for d in root.iterdir() if root.is_dir() else []:
        if d == folder or not d.is_dir() or not UPLOAD_ID.match(d.name):
            continue
        for g in d.rglob("*"):
            try:
                if g.is_file() and g.stat().st_size == size:
                    h = h or _sha256_16(f)
                    if _sha256_16(g) == h:
                        return True
            except OSError:
                continue
    return False


def _stop_jobs_on(folder: Path, wait: float = 3.0) -> list[int]:
    """T2 cycle 4: background jobs still reading this upload (the web server's warm-up `python -m stoa.formats.warm
    <folder>/<file>`, a takeoff / glance run, ...) would write its readings back AFTER Delete (measured: an upload
    deleted 1 s after arriving got its W3 text layer written 14 s later). Stop every process of this user whose
    arguments name a file in this folder (the shared answering workers take files on stdin, so they never match),
    SIGTERM then SIGKILL. Linux /proc; elsewhere a no-op. Returns the pids stopped."""
    import signal
    import time

    prefixes = {str(folder) + os.sep, str(uploads_root() / folder.name) + os.sep}
    me, uid, hits = os.getpid(), os.getuid(), []
    try:
        procs = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []
    for d in procs:
        pid = int(d)
        if pid == me:
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != uid:
                continue
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if any(a.startswith(pre) or a == pre.rstrip(os.sep) for a in argv[1:] for pre in prefixes):
            try:
                os.kill(pid, signal.SIGTERM)
                hits.append(pid)
            except OSError:
                pass
    deadline = time.time() + wait
    for pid in hits:
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return hits


LATE_SWEEPS = (15.0, 60.0, 180.0)  # seconds after Delete: readings written late by a job that was mid-way


def _late_plan(folder: Path) -> dict:
    """What a late writer could still create, recorded while the files exist: the stems of the folder's files and the
    strings their readings mention (the folder, the upload id)."""
    files = [p for p in folder.rglob("*") if p.is_file()] if folder.is_dir() else []
    try:
        from stoa.extras.room_graph.graph import map_name
    except Exception:  # noqa: BLE001 - same rule as derived()
        map_name = None
    content = []  # content-named readings (F2 takeoffs '<sha16>-p*', CC5K room maps '<map_name>.npz'), named now
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        maps = []
        if map_name is not None:
            try:
                name = map_name(str(f))
                if name != f.stem:
                    maps.append(name)
            except Exception:  # noqa: BLE001
                pass
        content.append({"sha": _sha256_16(f), "size": size, "maps": maps})
    tmp = Path(tempfile.gettempdir())
    return {"stems": sorted({f.stem[:60] for f in files}), "needles": [str(folder), folder.name], "uid": folder.name,
            "folder": str(folder), "dirs": [str(d) for d in _named_cache_dirs()],  # fixed now, not at sweep time
            "content": content, "takeoff": str(Path(os.environ.get("STOA_TAKEOFF_CACHE") or tmp / "stoa-takeoff")),
            "maps_dir": str(shared_root() / "artifacts" / "R1" / "cc5k_seg_live"),
            # cycle 5: W1's IFC index / parts names (they hash the file's size + mtime, unknown once it is gone)
            "ifc": [n for f in files for n in _ifc_names(f)],
            "ifc_stems": sorted({f.stem for f in files if f.suffix.lower() == ".ifc"}),
            # cycle 9: an upright copy / a picture's PNG copy finishing after the Delete (named by the path: name now)
            "upright": _upright_keys(files),
            "side": [str(Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "formats"
                         / hashlib.sha1(str(f.resolve()).encode()).hexdigest()[:12]) for f in files]}


def _bytes_elsewhere(root: Path, size: int, sha: str) -> bool:
    """Some upload folder under root holds a file of that size and content hash (the deleted one is gone already)."""
    for d in root.iterdir() if root.is_dir() else []:
        if not d.is_dir() or not UPLOAD_ID.match(d.name):
            continue
        for g in d.rglob("*"):
            try:
                if g.is_file() and g.stat().st_size == size and _sha256_16(g) == sha:
                    return True
            except OSError:
                continue
    return False


_WORK_DIRS = (".stoa_raw",)  # written INTO an upload folder by a reader / F7 while answering, never by an upload


def _only_work_files(folder: Path) -> bool:
    try:
        names = [c.name for c in folder.iterdir()]
    except OSError:
        return False
    return bool(names) and all(n in _WORK_DIRS for n in names)


def late_sweep(plan: dict) -> list[str]:
    """Delete readings of a deleted upload that appeared after the Delete (same rule as derived(): a cache named after
    one of its files that mentions its folder, or named with its upload id). Never touches a live upload: the folder
    must still be gone."""
    gone: list[str] = []
    folder = Path(plan["folder"])
    if folder.is_dir() and _only_work_files(folder):
        # cycle 7 (F7 01:26): a correction finishing after the Delete re-creates <folder>/.stoa_raw/ (F7's raw readings
        # for 15 min); a folder holding only such work files is not a re-upload
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            gone.append(str(folder))
    if folder.exists():
        return gone  # uploaded again since: its readings are live again
    for d in map(Path, plan["dirs"]):
        if not d.is_dir():
            continue
        found = [c for stem in plan["stems"] for c in d.glob(f"{glob_escape(stem)}*")
                 if c.is_file() and _mentions(c, plan["needles"])]
        found += [c for c in d.glob(f"*{plan['uid']}*") if c.is_file()]
        for c in found:
            try:
                c.unlink()
                gone.append(str(c))
            except OSError:
                pass
    if plan.get("ifc") or plan.get("ifc_stems"):  # cycle 5: a model's index / parts map finished after the Delete
        folder = Path(plan["folder"])
        stub = [folder / f"{s}.ifc" for s in plan.get("ifc_stems", [])]  # names only: the files are gone
        for c in _ifc_readings(folder, stub, plan.get("ifc", [])):
            try:
                c.unlink()
                gone.append(str(c))
            except OSError:
                pass
    for c in _upright_copies(plan.get("upright", [])) + [Path(s) for s in plan.get("side", []) if Path(s).is_dir()]:
        try:  # cycle 9
            shutil.rmtree(c) if c.is_dir() else c.unlink()
            gone.append(str(c))
        except OSError:
            pass
    if plan.get("memory"):
        _forget_in_memory(plan["memory"])  # cycle 5: readings cached in this process by a request that was mid-way
    take, maps = Path(plan.get("takeoff", "")), Path(plan.get("maps_dir", ""))
    for c in plan.get("content", []):
        if not c.get("sha") or _bytes_elsewhere(Path(plan["folder"]).parent, c["size"], c["sha"]):
            continue  # another upload of the same bytes uses these readings
        late = [t for t in take.glob(f"{c['sha']}-p*") if t.is_dir()] if take.is_dir() else []
        late += [m for m in (maps / f"{n}.npz" for n in c.get("maps", [])) if m.is_file()]
        for t in late:
            try:
                shutil.rmtree(t) if t.is_dir() else t.unlink()
                gone.append(str(t))
            except OSError:
                pass
    return gone


def _schedule_late_sweeps(plan: dict) -> None:
    import threading
    import time

    def run():
        t0 = time.time()
        for at in LATE_SWEEPS:
            time.sleep(max(0.0, at - (time.time() - t0)))
            try:
                late_sweep(plan)
            except Exception:  # noqa: BLE001 — a background sweep never raises
                pass

    threading.Thread(target=run, name=f"privacy-late-{plan['uid']}", daemon=True).start()


def _forget_sample_corrections(folder: Path, visitors: list[str] | None) -> int:
    """Cycle 8: an example sheet's folder is shared and stays, but F7 files every visitor's corrections on it INSIDE
    it (<folder>/.stoa_corrections.json, items tagged with the browser's visitor id), so 'Delete my files' there removed
    nothing of the visitor's while the note says corrections go with Delete. Drop this browser's items from that one
    store (F7's own forget() rule, scoped to this sheet); other visitors' items are never touched. Returns the count."""
    vis = {str(v).strip() for v in visitors or [] if str(v or "").strip() and str(v).strip() != "anon"}
    if not vis:
        return 0
    try:
        from stoa.features import corrections

        f = folder / getattr(corrections, "_FILE", ".stoa_corrections.json")
        if not f.is_file():
            return 0
        data = json.loads(f.read_text())
        proj = str(data.get("project") or "")
        items = list(data.get("items") or [])
        if not proj or corrections.store_path(proj).resolve() != f.resolve():
            return 0
        left = [dict(i) for i in items if i.get("visitor") not in vis]
        if len(left) == len(items):
            return 0
        corrections.save(proj, {**data, "items": left})
        return len(items) - len(left)
    except Exception:  # noqa: BLE001 — never fail the delete over a correction store
        return 0


def _forget_sample_project_lists(folder: Path, visitors: list[str] | None) -> int:
    """Cycle 8, same reason: T3's project plug-in keeps each browser's list of an example sheet's set in the shared
    folder as .stoa_project.v-<visitor>.json (seen live in the DfE example folder, 3 visitors). Remove this browser's
    file only (T3's own store_path / visitor_key rule); the shared .stoa_project.json and other visitors' stay."""
    n = 0
    try:
        from stoa.features import project
    except Exception:  # noqa: BLE001
        return 0
    for v in {str(v).strip() for v in visitors or [] if str(v or "").strip()}:
        try:
            if not project.visitor_key(v):
                continue
            f = project.store_path(folder.name, v)
            if f.parent.resolve() == folder.resolve() and f.name != project._FILE and f.is_file():
                f.unlink()
                n += 1
        except Exception:  # noqa: BLE001 — a bad id / an unreadable file never fails the delete
            continue
    return n


def _forget_sample_takeoff_fixes(folder: Path, visitors: list[str] | None) -> int:
    """Cycle 9, same reason as the two above: this browser's Measure fixes on an example sheet's takeoff (F2 files them
    per visitor in the content-keyed job folder, shared by every visitor). The job and other visitors' fixes stay."""
    take = Path(os.environ.get("STOA_TAKEOFF_CACHE") or Path(tempfile.gettempdir()) / "stoa-takeoff")
    if not take.is_dir() or not folder.is_dir():
        return 0
    dirs = [c for f in folder.rglob("*") if f.is_file() and (h := _sha256_16(f))
            for c in take.glob(f"{h}-p*") if c.is_dir()]
    n = 0
    for f in _takeoff_fix_files(dirs, visitors):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def delete_upload(ref: str, late: bool = True, visitors: list[str] | None = None) -> dict:
    folder = _upload_dir(ref)
    why = sample_reason(folder)
    if why:
        # STOA_PRIVACY_SAMPLE_FORGET=0: an example sheet's Delete touches nothing (as before cycle 8)
        mine = visitors if os.environ.get("STOA_PRIVACY_SAMPLE_FORGET", "1") != "0" else []
        forgot, lists = _forget_sample_corrections(folder, mine), _forget_sample_project_lists(folder, mine)
        fixes = _forget_sample_takeoff_fixes(folder, mine)
        return {"ok": True, "deleted": [], "sample": why,
                "kept": [{"where": str(folder), "why": f"not deleted: {why}"}],
                **({"forgot_corrections": forgot} if forgot else {}),
                **({"forgot_project_lists": lists} if lists else {}),
                **({"forgot_takeoff_fixes": fixes} if fixes else {})}
    stopped = _stop_jobs_on(folder)
    plan = _late_plan(folder)
    mem_plan = _memory_plan(folder, [p for p in folder.rglob("*") if p.is_file()] if folder.is_dir() else [])
    plan["memory"] = mem_plan  # a request still running at Delete time may cache a reading afterwards
    caches, kept = derived(folder, visitors)
    deleted: list[str] = []
    for c in caches:
        if not c.exists():
            continue  # inside a folder removed just before
        try:
            if c.is_dir():
                shutil.rmtree(c)
            else:
                c.unlink()
            deleted.append(str(c))
        except OSError:
            kept.append({"where": str(c), "why": "could not be removed (permissions)"})
    rc = _revcompare_root()
    _forget_compare_results([Path(d).name for d in deleted if Path(d).parent == rc])
    forgot = _forget_in_memory(mem_plan)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        deleted.insert(0, str(folder))
    if late and not folder.exists():
        _schedule_late_sweeps(plan)
    return {"ok": not folder.exists(), "deleted": deleted, "kept": kept,
            **({"stopped_jobs": len(stopped)} if stopped else {}), **({"forgotten_in_api": forgot} if forgot else {})}


def public_paths(text: str) -> str:
    """A path as a VISITOR may see it (T2 22:5x): the delete result listed absolute server paths, i.e. the operator's
    home folder, login name and directory layout ('/home/<login>/work/...'). Roots become role names: <uploads>,
    <team cache>, <tmp>, <home>; the part below the root (an upload id, a cache's own name) stays, so the result still
    says what was removed and where it lived."""
    roots = [(str(uploads_root()), "<uploads>"), (str(shared_root()), "<team cache>"),
             (tempfile.gettempdir(), "<tmp>"), (os.path.expanduser("~"), "<home>")]
    for r in sorted({r.rstrip("/") for r, _ in roots if r and r != "/"}, key=len, reverse=True):
        name = next(n for rr, n in roots if rr.rstrip("/") == r)
        text = re.sub(re.escape(r) + r"(?=/|$|[\s,;:'\")])", name, text)
    # the real data roots too (any other absolute path is cut to its last two parts)
    return re.sub(r"(?<![\w<>])/(?:home|data|srv|mnt|Users|root)/[^\s,;:'\"]*",
                  lambda m: "…/" + "/".join(m.group(0).rstrip("/").split("/")[-2:]), text)


@router.post("/delete")
def post_delete(req: DeleteReq) -> dict:
    try:
        r = delete_upload(req.upload, visitors=list(req.visitors or []))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=public_paths(str(e))) from None
    return {**r, "deleted": [public_paths(p) for p in r.get("deleted", [])],
            "kept": [{**k, "where": public_paths(str(k.get("where", ""))), "why": public_paths(str(k.get("why", "")))}
                     for k in r.get("kept", [])],
            **({"sample": public_paths(str(r["sample"]))} if r.get("sample") else {})}


start_sweeper()  # no-op unless STOA_UPLOAD_TTL_HOURS is set (the router module is imported only when 'privacy' is on)
