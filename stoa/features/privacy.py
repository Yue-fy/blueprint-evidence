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
about; revision-compare images, keyed by the pair compared) are listed under 'kept' with the reason, never silently
skipped.
"""

from __future__ import annotations

import hashlib
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
     "where": "this server's disk, a temporary folder named by the file's content (not shared with other visitors' "
              "names)", "kept": "until you press 'Delete my files' or the folder is cleared by the operator"},
    {"what": "Readings of your file (text found on the sheet, OCR, page renders, room labels)",
     "where": "this server's disk caches, so a second question is fast",
     "kept": "'Delete my files' removes the ones tied to your file, page renders and page copies included; crops of "
             "a region you asked about and cached model answers are named by a hash, not by your file, and stay "
             "until the operator clears the caches"},
    {"what": "An image of the page, the question and the proposed answer",
     "where": "Google Gemini (vision step / agent) or NVIDIA Nemotron (second opinion), ONLY when an answer is unsure "
              "and Private is off; each answer lists where its data went",
     "kept": "under the provider's API terms; never with Private on (no connection leaves this server then)"},
    {"what": "Your corrections, project groupings and takeoff lists",
     "where": "your upload's folder, or a cache named by your file's content",
     "kept": "until you press 'Delete my files' (it removes them with the file)"},
    {"what": "Questions and answers", "where": "the server's logs and response cache for this event",
     "kept": "for the event; no account, no tracking"},
]


def note() -> dict:
    priv = os.environ.get("STOA_PRIVATE", "").strip() == "1"
    text = ("Files you upload stay on this server (a temporary folder) and are read by our own readers and models "
            "running here. With Private off, an unsure answer may send one page image, the question and the proposed "
            "answer to Google Gemini or NVIDIA Nemotron; every answer says where its data went. With Private on, "
            "nothing leaves this server: the network is closed to outside addresses while it answers. "
            "'Delete my files' removes your upload and the readings tied to it.")
    items = [dict(i) for i in NOTE]
    ttl = ttl_hours()
    if ttl:
        text += f" Uploads are deleted automatically {ttl:g} hours after they arrive."
        items[0]["kept"] = (f"{ttl:g} hours after the upload (deleted automatically with its readings), or earlier "
                            "when you press 'Delete my files'")
    return {"text": text, "items": items, "private_mode_server_default": priv, "retention_hours": ttl or None}


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
            if not (d.is_dir() and UPLOAD_ID.match(d.name)) or (d / ".keep").exists():
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


def derived(folder: Path) -> tuple[list[Path], list[dict]]:
    """Cache files tied to the upload in `folder` (deleted with it) and the caches that cannot be tied (kept)."""
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
    for f in files:
        h = _sha256_16(f)
        if h:
            out += [c for c in takeoff.glob(f"{h}-p*") if c.is_dir()]
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
                out += [c for c in live.glob(f"{glob_escape(name)}.npz") if c.is_file()]
    kept = [{"where": str(shared_root() / "artifacts" / "llm-cache"),
             "why": "model responses are keyed by a hash of the request (image + question); they cannot be traced to "
                    "this file without the request, and hold answers, not the drawing"},
            {"where": ", ".join(str(p) for p in (shared_root() / "artifacts" / "VP" / "crops", tmp / "stoa-region",
                                                 shared_root() / "cache" / "det")),
             "why": "crops of a region of the page and region-masked copies are named by a hash that includes the "
                    "region / box asked about, which is not stored; they are not removed"},
            {"where": str(Path(os.environ.get("STOA_FEAT_CACHE", tmp / "stoa-feat")) / "revcompare"),
             "why": "revision-compare images are named by a hash of the PAIR of drawings compared; not removed"}]
    if map_name is None:
        kept.append({"where": str(shared_root() / "artifacts" / "R1" / "cc5k_seg_live"),
                     "why": "room maps of pictures are named by the file name alone (shared by uploads of the same "
                            "name); not removed"})
    return sorted(set(out)), kept


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


def delete_upload(ref: str) -> dict:
    folder = _upload_dir(ref)
    caches, kept = derived(folder)
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
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        deleted.insert(0, str(folder))
    return {"ok": not folder.exists(), "deleted": deleted, "kept": kept}


@router.post("/delete")
def post_delete(req: DeleteReq) -> dict:
    try:
        return delete_upload(req.upload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


start_sweeper()  # no-op unless STOA_UPLOAD_TTL_HOURS is set (the router module is imported only when 'privacy' is on)
