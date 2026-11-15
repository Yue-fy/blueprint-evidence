"""F7 — learn from corrections (prompts/F7.md): what a user fixes once is remembered for the project and reused.

A correction is one thing the user told us about a drawing:

- ``scale``   the drawing scale (1:N, or pixels per metre on a picture), set with the measure tool or the 1:N chips.
              Used for every later area / length question on that file, and on the other sheets of the same set that
              print no scale of their own (or print the same, wrong, one).
- ``choice``  the option they picked in a clarification ("Which plan do you mean?" -> "MODULE 01", "Which classroom?"
              -> "Secondary Classroom 3"). When a later answer asks the same thing again it is answered with their pick
              instead of asking again; with ``phrase`` ("the classroom") later questions using those words mean that room.
- ``symbol``  "this symbol is a door" (or "is not a door") at a box. Later counts of that kind on the sheet add the
              symbol we missed / drop the box we wrongly counted, and show it.
- ``value``   "the right answer is 12": the same question on that file answers their value, labelled as theirs.
- ``sheet``   the sheet's kind / floor / title as the user named it (the 'Change' menu): shown on later answers.

Memory: one JSON file per project, next to the uploaded files (``<STOA_UPLOADS>/<id>/.stoa_corrections.json``), or under
``STOA_CORRECTIONS_DIR`` for dataset drawings / explicit projects. Read again when another process changed it (the API
writes, the upload workers read). Every correction can be undone and redone (the undo list).

Hooks (fail closed: any error -> the request / answer passes unchanged):

    question, solver_args, ctx = before(path, question, solver_args)     # before the solver runs
    out = after(path, question, out, ctx, resolve=None)                  # on the answer dict (Answer.model_dump())

``after`` adds ``out["learned"] = [{id, kind, text, changed}]`` (the UI shows "Improved using your correction") and puts
the same sentence first in ``assumptions``. Turned on by ``STOA_FEATURES`` containing ``corrections`` (or ``all``), or
per request with ``solver_args["learn"]`` (any value; it also keys the answer caches, so the UI sends the store's
revision there and a new correction is never hidden by a cached answer).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

KINDS = ("scale", "choice", "symbol", "value", "sheet", "room")
FLAG = "corrections"
_FILE = ".stoa_corrections.json"
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}  # store file -> (mtime, data)
PT_PER_M = 72 / 0.0254  # PDF points per metre of paper
_OBJECT_WORDS = re.compile(
    r"(?i)(?:(?:whole|entire|total|same|other|first|second|this|that)\s+)?(?:doors?|windows?|rooms?|spaces?|plans?|views?|"
    r"sheets?|drawings?|pages?|floors?(?:\s+plans?)?|levels?|storeys?|stories|areas?|lengths?|walls?|stairs?|columns?|"
    r"toilets?|fixtures?|outlets?|lights?|number|count|total|building|model|one|ones|answer|value|scale)")
# a clarification between two READINGS of the same thing (G1's 'Two readings disagree: which area do you mean?')
_READING_ASK = re.compile(r"(?i)\b(disagree\w*|readings?|which (?:area|value|number|length|count|size) do you mean)\b")


# -- feature flag ----------------------------------------------------------------------------------------------------
def enabled(solver_args: dict | None = None) -> bool:
    """On when STOA_FEATURES lists 'corrections' / 'all', or the request carries solver_args['learn'] (not 0 / off)."""
    if solver_args and "learn" in solver_args:
        return str(solver_args.get("learn")).lower() not in ("0", "", "off", "false", "none")
    flags = {f.strip().lower() for f in os.environ.get("STOA_FEATURES", "").split(",")}
    return FLAG in flags or "all" in flags


# -- where a project's memory lives ------------------------------------------------------------------------------------
def uploads_root() -> Path:
    return Path(os.environ.get("STOA_UPLOADS", Path(tempfile.gettempdir()) / "stoa-uploads"))


def memory_dir() -> Path:
    d = os.environ.get("STOA_CORRECTIONS_DIR")
    if d:
        return Path(d)
    shared = os.environ.get("JUNCTION_SHARED")
    return Path(shared) / "artifacts" / "F7" / "memory" if shared else Path(tempfile.gettempdir()) / "stoa-corrections"


_UP = re.compile(r"^upload/([0-9a-f]{12})/(?:([a-z0-9_]+)/)?([^/\\]+)$")


def file_id(path: str) -> str:
    """The name the browser knows a file by: 'upload/<id>/<name>' for uploads (also from their absolute path in the
    upload folder), the dataset-relative path otherwise."""
    p = str(path or "")
    if p.startswith("upload/"):
        return p
    try:
        ap = Path(p).resolve()
        root = uploads_root().resolve()
        if root in ap.parents:
            parts = ap.relative_to(root).parts  # <id>/<name> or <id>/data/<style>/<name>
            if len(parts) == 2:
                return f"upload/{parts[0]}/{parts[1]}"
            if len(parts) == 4 and parts[1] == "data":
                return f"upload/{parts[0]}/{parts[2]}/{parts[3]}"
    except (OSError, ValueError):
        pass
    try:
        from stoa.data import dataset_rel

        return dataset_rel(p)
    except Exception:  # noqa: BLE001
        return p


def project_of(path: str, project: str | None = None) -> str:
    """The project a file belongs to: an explicit project id, else its upload folder ('upload/<id>': files uploaded
    together = a set), else the drawing's folder in the dataset."""
    if project:
        return re.sub(r"[^A-Za-z0-9_./-]", "_", str(project))[:200]
    fid = file_id(path)
    m = _UP.match(fid)
    if m:
        return f"upload/{m.group(1)}"
    return str(Path(fid).parent)


def store_path(project: str) -> Path:
    m = re.fullmatch(r"upload/([0-9a-f]{12})", project)
    if m and (uploads_root() / m.group(1)).is_dir():
        return uploads_root() / m.group(1) / _FILE
    return memory_dir() / f"{hashlib.sha1(project.encode()).hexdigest()[:16]}.json"


def _empty(project: str) -> dict:
    return {"version": 1, "project": project, "rev": 0, "items": []}


def load(project: str) -> dict:
    sp = store_path(project)
    try:
        mt = sp.stat().st_mtime
    except OSError:
        return _empty(project)
    with _LOCK:
        hit = _CACHE.get(str(sp))
        if hit and hit[0] == mt:
            return hit[1]
    try:
        data = json.loads(sp.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = _empty(project)
    except (OSError, ValueError):
        data = _empty(project)
    with _LOCK:
        _CACHE[str(sp)] = (mt, data)
    return data


def save(project: str, data: dict, bump: bool = True) -> dict:
    """Write atomically; `bump` = a real change (new revision: the UI's answer-cache key), not bookkeeping."""
    sp = store_path(project)
    sp.parent.mkdir(parents=True, exist_ok=True)
    if bump:
        data["rev"] = int(data.get("rev", 0)) + 1
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = sp.with_name(f".{sp.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, sp)  # atomic: a reader in another process never sees half a file
    with _LOCK:
        _CACHE[str(sp)] = (sp.stat().st_mtime, data)
    return data


# -- recording corrections ---------------------------------------------------------------------------------------------
def _norm_q(q: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.²]", " ", str(q or "").lower())).strip()


def _ratio_of(item: dict) -> int | None:
    try:
        r = item.get("ratio")
        if r not in (None, "", 0):
            m = re.fullmatch(r"\s*(?:1\s*:\s*)?(\d{1,6}(?:\.\d+)?)\s*", str(r))
            return int(round(float(m.group(1)))) if m else None
        upm, space = item.get("units_per_m"), item.get("space")
        if upm and space == "pdf_pt":
            return int(round(PT_PER_M / float(upm)))
    except (TypeError, ValueError):
        return None
    return None


def _printed_scale(fid: str) -> str | None:
    """The scale the sheet PRINTS (stoa.router.recognize, content only, cached), or None."""
    rec = _recognize(fid)
    if rec and rec.get("scale_source") == "printed" and rec.get("scale"):
        return str(rec["scale"])
    return None


def _abs(fid: str) -> str:
    m = _UP.match(fid)
    if m:
        return str(uploads_root() / m.group(1) / ("data/" + m.group(2) + "/" if m.group(2) else "") / m.group(3))
    from stoa.data import abspath

    return str(abspath(fid))


_REC: dict[str, dict | None] = {}


def _recognize(fid: str) -> dict | None:
    if fid in _REC:
        return _REC[fid]
    try:
        from stoa.router.recognize import recognize

        rec = recognize(_abs(fid))
    except Exception:  # noqa: BLE001
        rec = None
    _REC[fid] = rec
    return rec


def add(path: str, kind: str, payload: dict, project: str | None = None) -> dict:
    """Remember one correction; returns the new item. A new scale / sheet name / value for the same file (and, for
    choices, the same question back) replaces the active one (the old one stays in the undo list, undone)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    fid, proj = file_id(path), project_of(path, project)
    item = {"id": uuid.uuid4().hex[:10], "kind": kind, "file": fid, "page": payload.get("page"),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "active": True, "applied": 0, "applied_to": []}
    for k, v in payload.items():
        if k not in item and k not in ("path", "project", "kind"):
            item[k] = v
    item["visitor"] = str(item.get("visitor") or "") or None
    if kind == "scale":
        n = _ratio_of(item)
        ppm = item.get("px_per_m") or (item.get("units_per_m") if item.get("space") == "image_px" else None)
        if not n and not ppm:
            raise ValueError("a scale needs ratio (1:N), px_per_m, or units_per_m + space (pdf_pt | image_px)")
        item["ratio"], item["px_per_m"] = n, (float(ppm) if ppm else None)
        item["printed"] = _printed_scale(fid)  # what the sheet itself prints (a wrong print applies set-wide)
        item.setdefault("scope", "set")
    elif kind == "choice":
        if not item.get("label"):
            raise ValueError("a choice needs the label the user picked")
        item["ask_norm"] = _norm_q(item.get("ask", ""))
        # a phrase is a NAME the user meant ('the kitchen' -> Kitchen 1.05), never the thing counted / measured or the
        # sheet itself ('Count the doors on this plan' -> 'doors' would turn 'How many doors…' into nonsense)
        ph = str(item.get("phrase") or "").strip()
        if ph and (_OBJECT_WORDS.fullmatch(ph) or len(ph) < 3):
            item["phrase"] = None
        # WHAT was picked: a scope (a plan / view / room / sheet: the option re-asks a narrower question) carries over
        # to later questions on the file; a READING ('Two readings disagree: which area?', the option holds a value,
        # labels like 'our area_tags reading') is about this one question: it applies to the same question again (or
        # the same task on the same named room), never to another room's disagreement
        asked = str(item.get("asked") or "").strip()
        said = f"{item.get('ask') or ''} {item.get('why') or ''}"
        if _READING_ASK.search(said) or (asked and _norm_q(item.get("question") or "") == _norm_q(asked)):
            item["pick"] = "reading"
            item["asked_norm"] = _norm_q(asked or item.get("question") or "")
            item["task"] = _task_of(asked or item.get("question") or "")
            item["subject"] = _subject_of(asked or item.get("question") or "")
            # a reading's label names a READER ('our area_tags reading'), never a room: no alias (G1's reading clarify
            # says click 'room', so a client may well send 'kitchen' -> 'How big is the our area_tags reading?')
            item["phrase"] = None
        else:
            item["pick"] = "scope"
    elif kind == "symbol":
        if not item.get("label") or not item.get("bbox"):
            raise ValueError("a symbol needs label ('door') and bbox [x0, y0, x1, y1]")
        item["label"] = str(item["label"]).lower().strip()
        item["is"] = bool(item.get("is", True))
        if item.get("bboxes"):  # 'N more like it': the mark first, then the look-alikes the user accepted; for 'this
            # is not a …': the rejected box first, then the counted boxes that look like it (alike())
            bbs = [list(map(float, b)) for b in item["bboxes"] if isinstance(b, (list, tuple)) and len(b) == 4]
            if not bbs:
                raise ValueError("bboxes: a list of [x0, y0, x1, y1]")
            item["bboxes"] = bbs[:SIMILAR_CAP + 1]
    elif kind == "room":
        # 'The gym is here': the visitor's word for a room ('gym', 'nurse's room', 'the Store' when there are several)
        # + where it is. Later questions naming it are asked about the room at that point (see before())
        ph = _clean_phrase(item.get("phrase") or _room_phrase(item.get("question") or "") or "")
        pt = item.get("point")
        if not ph:
            raise ValueError("a room needs the phrase the visitor used ('gym') or a question naming it")
        if not (isinstance(pt, (list, tuple)) and len(pt) >= 2 and all(isinstance(v, (int, float)) for v in pt[:2])):
            raise ValueError("a room needs point [x, y] (in space pdf_pt | image_px)")
        item["phrase"], item["point"] = ph, [round(float(pt[0]), 2), round(float(pt[1]), 2)]
        item["space"] = item.get("space") or ("pdf_pt" if fid.lower().endswith(".pdf") else "image_px")
        item["page"] = int(item.get("page") or 0)
        if not item.get("label"):  # what is printed there (a hint for the panel: the readers resolve the room themselves)
            item.update(_room_hint(fid, item["page"], item["point"], item["space"]))
    elif kind == "value":
        if item.get("value") in (None, "") or not item.get("question"):
            raise ValueError("a value correction needs question and value")
        item["question_norm"] = _norm_q(item["question"])
        # what the number is OF, so a reworded question finds it too ('How big is the Kitchen?' after 'What is the
        # floor area of the Kitchen?'): the client's subject (the room the user measured), else read from the question
        item["task"] = item.get("task") or _task_of(item["question"])
        # lower-case room words count too ('the main hall', 'the gym'): a reworded question and the room's lengths find it
        item["subject"] = str(item.get("subject") or _subject_of(item["question"]) or _room_phrase(item["question"])
                              or "").strip() or None
    data = load(proj)
    data = {**data, "items": [dict(i) for i in data["items"]]}
    for old in data["items"]:
        if not old.get("active") or old["kind"] != kind or old["file"] != fid or old.get("visitor") != item["visitor"]:
            continue
        same = (kind == "scale" or (kind == "sheet" and bool(old.get("read_as")) == bool(item.get("read_as")))
                or (kind == "value" and old.get("question_norm") == item.get("question_norm"))
                or (kind == "room" and str(old.get("phrase") or "").lower() == str(item.get("phrase") or "").lower())
                or (kind == "choice" and old.get("ask_norm") == item.get("ask_norm") and old.get("phrase") == item.get("phrase")
                    and (item.get("pick") != "reading" or (old.get("asked_norm"), old.get("subject"))
                         == (item.get("asked_norm"), item.get("subject"))))
                or (kind == "symbol" and old.get("label") == item["label"] and (_iou(old.get("bbox"), item["bbox"]) > 0.5 or (
                    item.get("bboxes") and bool(old.get("is")) == item["is"] and _overlaps(old.get("bbox"), item["bbox"])))))
        if same:
            old["active"], old["replaced_by"] = False, item["id"]
    data["items"].append(item)
    save(proj, data)
    return item


def set_active(path: str, item_id: str, active: bool, project: str | None = None) -> dict | None:
    proj = project_of(path, project)
    data = load(proj)
    data = {**data, "items": [dict(i) for i in data["items"]]}
    hit = None
    for it in data["items"]:
        if it["id"] == item_id:
            it["active"], hit = bool(active), it
            if active:  # redo: the item it replaced (or that replaced it) for the same thing goes back off
                for o in data["items"]:
                    if o is not it and o.get("active") and (o.get("replaced_by") == it["id"] or it.get("replaced_by") == o["id"]):
                        o["active"] = False
    if hit is None:
        return None
    save(proj, data)
    return hit


def remove(path: str, item_id: str, project: str | None = None) -> bool:
    proj = project_of(path, project)
    data = load(proj)
    keep = [dict(i) for i in data["items"] if i["id"] != item_id]
    if len(keep) == len(data["items"]):
        return False
    save(proj, {**data, "items": keep})
    return True


def forget(visitor: str, keep: list[str] | None = None) -> dict:
    """W5's 'Start over' (captain 01:18): drop EVERY correction this visitor made — catalogue cases and dataset drawings
    always; the projects of the paths in `keep` (their own uploads, when they did not confirm) stay. Other visitors'
    items are never touched. Returns {removed, projects}."""
    v = str(visitor or "").strip()
    if not v:
        raise ValueError("forget needs the visitor id")
    kept = {project_of(k) for k in keep or [] if k}
    files = [*memory_dir().glob("*.json"), *uploads_root().glob(f"*/{_FILE}")] if uploads_root().is_dir() else [*memory_dir().glob("*.json")]
    removed, projects = 0, []
    for f in files:
        try:
            data = json.loads(f.read_text())
            proj = str(data.get("project") or "")
            if not proj or proj in kept or store_path(proj) != f:
                continue
            left = [dict(i) for i in data.get("items") or [] if i.get("visitor") != v]
            if len(left) == len(data.get("items") or []):
                continue
            removed += len(data["items"]) - len(left)
            projects.append(proj)
            save(proj, {**data, "items": left})
        except Exception:  # noqa: BLE001 — one unreadable store never stops the rest
            continue
    return {"removed": removed, "projects": projects}


def items_for(path: str, project: str | None = None, active_only: bool = True, visitor: str | None = None) -> list[dict]:
    """The project's corrections that belong to this VISITOR (a random id per browser, sent by the plugin): uploads are
    keyed by content, so on a shared demo every judge who drops the same files lands in the same project folder, and one
    judge's corrections must never change (or be credited to) the next one's answers (QA 21:43 / 21:47). Items without
    a visitor (scripts, tests, other features writing here) only apply to requests without one."""
    data = load(project_of(path, project))
    v = str(visitor or "") or None
    return [i for i in data["items"] if (i.get("active") or not active_only) and (i.get("visitor") or None) == v]


def _note_applied(project: str, used: list[dict], fid: str, question: str) -> None:
    if not used:
        return
    try:
        data = load(project)
        data = {**data, "items": [dict(i) for i in data["items"]]}
        ids = {u["id"] for u in used if u.get("changed", True) or u.get("confirmed")}
        for it in data["items"]:
            if it["id"] in ids:
                it["applied"] = int(it.get("applied", 0)) + 1
                hist = [h for h in it.get("applied_to", []) if h.get("question") != question or h.get("file") != fid]
                it["applied_to"] = [*hist, {"file": fid, "question": question[:200], "at": time.strftime("%H:%M:%S")}][-12:]
        save(project, data, bump=False)  # applied counts are bookkeeping: the revision (cache key) stays
    except Exception:  # noqa: BLE001
        pass


# -- geometry helpers --------------------------------------------------------------------------------------------------
def _iou(a, b) -> float:
    try:
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / ua if ua > 0 else 0.0
    except (TypeError, IndexError):
        return 0.0


def _overlaps(a, b) -> bool:
    """Same symbol: IoU > 0.25, or the centre of one inside the other."""
    if _iou(a, b) > 0.25:
        return True
    try:
        for p, q in ((a, b), (b, a)):
            cx, cy = (p[0] + p[2]) / 2, (p[1] + p[3]) / 2
            if q[0] <= cx <= q[2] and q[1] <= cy <= q[3]:
                return True
    except (TypeError, IndexError):
        pass
    return False


# -- hooks -------------------------------------------------------------------------------------------------------------
_AREA_LEN = re.compile(r"(?i)\b(area|size|how big|square|m2|m²|sq ?m|sq ?ft|sf\b|ft2|length|long|wide|width|perimeter|"
                       r"distance|skirting|base ?boards?|linear|running|metres|meters|feet|lf\b|fläche|länge|pinta-ala|pituus)")
_WORDS = {"door": r"doors?|doorways?|doorsets?", "window": r"windows?", "toilet": r"toilets?|wcs?\b|lavatories",
          "stair": r"stairs?|staircases?|stairways?", "column": r"columns?|pillars?", "sink": r"sinks?|basins?"}


def _scale_for(fid: str, items: list[dict]) -> tuple[dict | None, str]:
    """The scale correction that applies to this file: its own, else a set-wide one from another sheet (only when
    this sheet prints no scale, or prints the same one the user overrode there)."""
    own = [i for i in items if i["kind"] == "scale" and i["file"] == fid]
    if own:
        return own[-1], "this drawing"
    others = [i for i in items if i["kind"] == "scale" and i["file"] != fid and i.get("ratio") and i.get("scope") != "file"]
    if not others:
        return None, ""
    it = others[-1]
    printed = _printed_scale(fid)
    if printed is None or (it.get("printed") and printed.replace(" ", "") == str(it["printed"]).replace(" ", "")):
        return it, f"another sheet of this set ({Path(it['file']).name})"
    return None, ""


def _shrink_factor(fid: str) -> float:
    """Pixel factor of the copy the readers see for a big picture (P1's shrunk copy; 1.0 = read as uploaded)."""
    try:
        from stoa.formats import normalize as N

        src = Path(_abs(fid))
        if src.suffix.lower() not in (".png", ".jpg", ".jpeg") or not hasattr(N, "shrunk_copy"):
            return 1.0
        got = N.shrunk_copy(src, getattr(N, "IMAGE_MAX_SIDE", 6000))
        return float(got[1]) if got and 0 < float(got[1]) < 1 else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _stamped(fid: str, n: int) -> str | None:
    """A copy of a PDF sheet that prints 'SCALE 1:n' (bottom right, small grey text: the text layer the scale readers
    parse), cached next to the upload as a hidden file (the upload folder lists no dot-files) or under the memory dir.
    None for non-PDFs or on any error."""
    try:
        src = Path(_abs(fid))
        if src.suffix.lower() != ".pdf" or not src.is_file():
            return None
        key = hashlib.sha1(f"{src}|{src.stat().st_size}|{src.stat().st_mtime_ns}|{n}".encode()).hexdigest()[:10]
        folder = src.parent if uploads_root().resolve() in src.resolve().parents else memory_dir() / "stamped"
        dst = folder / f".{src.stem}.f7-1to{n}-{key}.pdf"
        if dst.is_file():
            return str(dst)
        import pymupdf

        folder.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(src) as doc:
            # only sheets with a real text layer (>= 20 characters per page, stoa.formats.normalize.has_text's rule): on
            # one whose text is drawn as outlines our one line could flip that test and skip its OCR pre-read
            if any(len((pg.get_text() or "").strip()) < 20 for pg in doc):
                return None
            for pg in doc:
                r = pg.rect
                pg.insert_text((max(4.0, r.width - 150), r.height - 6), f"SCALE 1:{n}", fontsize=6, color=(0.55, 0.55, 0.55))
            tmp = dst.with_name(dst.name + f".{os.getpid()}.tmp")
            doc.save(tmp, garbage=1, deflate=True)
        os.replace(tmp, dst)
        return str(dst)
    except Exception:  # noqa: BLE001
        return None


def before(path: str, question: str, solver_args: dict | None = None, project: str | None = None, page: int | None = None,
           context: dict | None = None) -> tuple[str, dict, dict]:
    """Apply what the user taught us BEFORE the solver runs: their scale (solver_args scale / px_per_m, unless the
    request sets one itself), room aliases from earlier picks, and rooms they pointed at ('The gym is here': the question
    is asked about the room at that point; ctx['context'] = the point for the upload worker, unless the request points
    at something itself). `page` = the page on screen (a room pointed at on another page does not apply). Returns
    (question, solver_args, ctx for after())."""
    args = dict(solver_args or {})
    ctx = {"used": [], "on": False, "project": None, "file": None, "asked": question, "page": page}
    visitor = args.pop("visitor", None)
    # the visitor just chose 'Read it as detected' (the undo of the remembered reader may still be on its way)
    auto = bool(args.pop("read_as_off", None))
    if not enabled(args):
        args.pop("learn", None)
        return question, args, ctx
    args.pop("learn", None)
    project = project or args.pop("learn_project", None)
    try:
        fid, proj = file_id(path), project_of(path, project)
        items = items_for(path, project, visitor=visitor)
        ctx.update(on=True, project=proj, file=fid, items=items)
        if not items:
            return question, args, ctx
        ra = [i for i in items if i["kind"] == "sheet" and i["file"] == fid and i.get("read_as")]
        if ra and not auto:  # the visitor's 'Change' -> 'Read it as …' on the recognition chip (wrap_upload: the solver)
            ctx["read_as"] = {"id": ra[-1]["id"], "solver": str(ra[-1]["read_as"]), "label": ra[-1].get("label") or ra[-1]["read_as"]}
        it, where = _scale_for(fid, items)
        if it and _AREA_LEN.search(question) and "scale" not in args and "px_per_m" not in args:
            if it.get("px_per_m") and it["file"] == fid:
                # the visitor measured on the picture they uploaded; a big one is read through a smaller copy
                # (stoa.formats.normalize.shrunk_copy, IMAGE_MAX_SIDE): their pixels per metre x that copy's factor
                f = _shrink_factor(fid)
                args["px_per_m"] = round(float(it["px_per_m"]) * f, 4)
                ctx["used"].append({"id": it["id"], "kind": "scale", "where": where, "px": True,
                                    "text": f"your scale (1 m = {it['px_per_m']:.1f} px, set on {where})",
                                    **({"read_at": f} if f != 1.0 else {})})
            if it.get("ratio") and not args.get("px_per_m"):
                n = int(it["ratio"])
                u = {"id": it["id"], "kind": "scale", "where": where, "ratio": n,
                     "text": f"your scale 1:{n} (set on {where})"}
                copy = _stamped(fid, n) if _printed_scale(fid) is None else None
                if copy:
                    # a sheet that prints NO scale: most readers then give up (plan_rooms abstains) or guess, so a
                    # rescale afterwards has nothing to fix. They read a copy that prints the user's scale instead
                    # (same pages, same coordinates: evidence maps back 1:1); after() says the scale was the user's
                    ctx["abs"], u["stamped"] = copy, True
                else:
                    args["scale"] = f"1:{n}"  # it prints another scale: rescale the reading (Pipeline._apply_user_scale)
                ctx["used"].append(u)
        q2 = question
        for c in items:
            ph, lab = str(c.get("phrase") or "").strip(), str(c.get("label") or "").strip()
            if c["kind"] != "choice" or not ph or not lab or c["file"] != fid or _OBJECT_WORDS.fullmatch(ph):
                continue
            if re.search(rf"(?i)\b{re.escape(lab)}\b", q2):
                continue
            pat = re.compile(rf"(?i)\b(?:the\s+)?{re.escape(ph)}\b")
            if pat.search(q2):
                q2 = pat.sub(f"the {lab}", q2, count=1)
                ctx["used"].append({"id": c["id"], "kind": "choice", "text": f"'{ph}' = {lab} (you picked it earlier)",
                                    "changed": True})
        if not context:
            q2 = _apply_rooms(fid, q2, items, ctx, page)
        return q2, args, ctx
    except Exception as e:  # noqa: BLE001 — fail closed
        ctx["error"] = f"{type(e).__name__}: {e}"
        return question, args, ctx


def after(path: str, question: str, out: dict, ctx: dict, resolve=None) -> dict:
    """Apply what the user taught us to the answer dict (Answer.model_dump() + extras) and label it. `resolve(q)` may
    re-ask a question (returns an answer dict) for a remembered clarification pick whose option carries no value."""
    if not ctx or not ctx.get("on") or not isinstance(out, dict):
        return out
    try:
        fid = ctx["file"]
        items = ctx.get("items") or []
        learned: list[dict] = []
        for u in ctx.get("used", []):
            if u["kind"] == "scale" and u.get("stamped"):
                learned.append(_unstamp(u, out, ctx))
            elif u["kind"] == "scale":
                learned.append(_scale_outcome(u, out))
            elif u["kind"] == "room":
                learned.append(_room_named(out, u, ctx.get("project")))
            else:
                learned.append(u)
        n_values = sum(1 for x in learned if x.get("kind") == "value")
        out = _apply_values(fid, question, out, items, learned, page=ctx.get("page"))
        if sum(1 for x in learned if x.get("kind") == "value") == n_values:  # no value of their own for this question
            out = _apply_outline(fid, question, out, items, learned, page=ctx.get("page"))
        out = _apply_symbols(fid, question, out, items, learned)
        out = _apply_choice(fid, question, out, items, learned, resolve)
        _apply_sheet(fid, out, items, learned)
        if learned:
            out["learned"] = [{k: v for k, v in x.items() if k in ("id", "kind", "text", "changed", "confirmed")} for x in learned]
            lead = [x for x in learned if x.get("changed")]
            if lead:  # no number out (the reader the user chose cannot answer this): used, nothing 'improved'
                head = "Improved using your correction" if out.get("value") not in (None, "") else "Used your correction"
                out["assumptions"] = [f"{head}: " + "; ".join(x["text"] for x in lead),
                                      *(out.get("assumptions") or [])]
            out["trace"] = [*(out.get("trace") or []), *(f"learn: {x['text']}" for x in learned)]
            _note_applied(ctx["project"], learned, fid, ctx.get("asked") or question)
        return out
    except Exception as e:  # noqa: BLE001 — fail closed: the answer as it was
        out.setdefault("trace", []).append(f"learn: skipped ({type(e).__name__})")
        return out


def _rec_line(t: str, n: int) -> str:
    """'recognised: {json}' (G1's recognise trace line, a UI fallback): scale_source printed -> user for the user's 1:n."""
    try:
        d = json.loads(t[len("recognised: "):])
        if str(d.get("scale") or "").replace(" ", "") == f"1:{n}" and d.get("scale_source") == "printed":
            d["scale_source"] = "user"
            return "recognised: " + json.dumps(d, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return t


def _unstamp(u: dict, out: dict, ctx: dict) -> dict:
    """The answer was read on the copy that prints the user's scale: say the scale is theirs, not printed; evidence
    and recognition point at the user's file again."""
    n = u["ratio"]
    rx = re.compile(rf"1\s*:\s*{n}\b(\s+(?:as\s+)?printed(?:\s+on\s+the\s+(?:sheet|drawing))?)?")
    mine = f"1:{n} set by you (the drawing prints no scale)"
    pre = re.compile(rf"\bprinted\s+(?=1\s*:\s*{n}\b)")  # '[plan_rooms] … sheet at printed 1:200' -> 'at 1:200 …'
    hit = False
    for key in ("assumptions", "trace", "missing"):
        new = []
        for t in out.get(key) or []:
            if isinstance(t, str) and t.startswith("recognised: {"):
                t2 = _rec_line(t, n)  # the JSON the UI parses: keep its scale, say whose it is
            else:
                t2 = rx.sub(mine, pre.sub("", str(t))) if isinstance(t, str) and "1:" in t else t
            hit |= t2 != t
            new.append(t2)
        out[key] = new
    copy = Path(ctx.get("abs") or "").name
    for e in out.get("evidence") or []:
        if copy and Path(str(e.get("file") or "")).name == copy:
            e["file"] = ctx["file"]
        if isinstance(e.get("note"), str):
            e["note"] = rx.sub(mine, e["note"])
    rec = out.get("recognition")
    if isinstance(rec, dict) and str(rec.get("scale") or "").replace(" ", "") == f"1:{n}":
        rec["scale_source"] = "user"
    measured = out.get("value") is not None and str(out.get("unit") or "").lower() in ("m2", "m²", "m", "ft2", "ft", "mm", "cm")
    x = {**u, "changed": bool(measured or hit)}
    if not x["changed"]:
        x["text"] += " — not used (this answer does not depend on the drawing scale)"
    return x


_FIXED_UNITS = ("dxf", "ifc", "area_tags", "area_table", "label_count", "stated_count", "schedule", "pdf_text", "prior",
                "slab_area")


def _measured(out: dict) -> bool:
    """An area / length measured on the drawing (not printed on it, not read from CAD / a model / a table)."""
    if out.get("value") is None or str(out.get("unit") or "").lower() not in ("m2", "m²", "m", "ft2", "ft", "mm", "cm"):
        return False
    if str(out.get("solver") or "").split("/")[-1] in _FIXED_UNITS:
        return False
    return not any(re.search(r"(?i)printed|as printed|schedule|ifc", str(e.get("note") or "")) for e in out.get("evidence") or [])


def _scale_outcome(u: dict, out: dict) -> dict:
    tr = " ".join(str(t) for t in out.get("trace") or [])
    asm = " ".join(str(a) for a in out.get("assumptions") or [])
    x = {**u}
    # a typical-size guess ('≈ 55.7 m² a typical size for a Music Room', the prior) measured nothing: the user's scale
    # did not make it (QA 21:35: the green 'Improved using your correction' sat under a rough guess)
    said = " ".join([tr, asm, " ".join(str(m) for m in out.get("missing") or []),
                     " ".join(str(e.get("note") or "") for e in out.get("evidence") or [])])
    located = any(e.get("bbox") or e.get("polygon") or e.get("polyline") for e in out.get("evidence") or [])
    guess = (str(out.get("solver") or "").split("/")[-1] in ("prior", "dummy")  # = the card's 'a typical size for this'
             or (out.get("confidence") is not None and float(out["confidence"]) < 0.05 and not located)  # needsMeasuring
             or re.search(r"(?i)\btypical (?:size|sizes|area|room|value)|\bsized like a typical\b", said))
    if guess and "applied (reading used" not in tr:
        x["changed"] = False
        x["text"] += " — not used (this answer is a typical-size guess, not measured on the drawing)"
    elif "applied (reading used" in tr or "scale set by the user" in asm:
        x["changed"] = True
        m = re.search(r"reading used 1:(\d+)", tr)
        if m:
            x["text"] += f"; the drawing was read at 1:{m.group(1)}"
    elif "confirmed by the user" in asm:
        x["changed"], x["text"] = False, x["text"] + " — same as the drawing's own"
    elif u.get("px") and _measured(out):
        # a picture: every raster measurement inside the solve ran at the user's pixels per metre (O1's user_scale
        # context), also the agent's tools, whose answers do not always say so (F7 image session: laundry / library)
        x["changed"] = True
        x["text"] += "; measured at it"
    else:
        x["changed"] = False
        x["text"] += " — not used (this answer does not depend on the drawing scale)"
    return x


_SUBJ = re.compile(r"\b(?:[Oo]f|[Ii]n|[Ff]or|[Dd]oes|[Ii]s|[Aa]re)\s+(?:[Tt]he\s+|[Tt]his\s+)?((?:[A-Z0-9][\w'&/-]*)(?:\s+[A-Z0-9][\w'&/-]*){0,4})")


def _subject_of(question: str) -> str | None:
    """The named thing a question is about: 'the Multi-Purpose Hall', 'the Kitchen', 'Room 204' (capitalised words).
    A trailing 'Room' / 'Area' stays: it is part of the name ('Break Room', 'Store Room', 'Waiting Area'; cutting it
    showed 'for the Break' on AFCEC A-101 and would let 'the Store' match 'Store Room')."""
    for m in _SUBJ.finditer(str(question or "")):
        sub = m.group(1).strip()
        if sub and sub.lower() not in ("the", "this", "it", "there", "what", "how"):
            return sub
    return None


def _task_of(question: str) -> str | None:
    q = str(question or "")
    if _BASE_Q.search(q) or re.search(r"(?i)\b(skirting|base ?boards?|perimeter|length|long|wide|width|linear|running)\b", q):
        return "length"
    if re.search(r"(?i)\bhow many\s+(square|sq\.?\s|m2|m²|ft2|ft²)", q):  # 'how many square metres is it?'
        return "area"
    if re.search(r"(?i)\b(how many|number of|count)\b", q):
        return "count"
    if re.search(r"(?i)\b(area|how big|size|square|m2|m²|sq ?m|sq ?ft|ft2)\b", q):
        return "area"
    return None


# a kept value is stored in the unit of the question it answered (the plug-in keeps a Measure result in ft² when the
# answer on screen was ft²: Stoa's AFCEC / USDA sheets); a later question that NAMES another unit gets it converted
_UNIT_K = {("m2", "ft2"): 10.7639104, ("ft2", "m2"): 1 / 10.7639104, ("m", "ft"): 1 / 0.3048, ("ft", "m"): 0.3048}
_SHOWN = {"m2": "m²", "ft2": "ft²", "m3": "m³", "count": ""}


def _unit_key(u) -> str:
    t = str(u or "").lower().replace("²", "2").replace(" ", "")
    return {"sqm": "m2", "sf": "ft2", "sqft": "ft2", "lf": "ft", "feet": "ft", "foot": "ft"}.get(t, t)


def _asked_unit(question: str) -> str | None:
    """The unit a question names explicitly ('in square feet', 'how many sq ft', 'in metres'); None = not said."""
    q = str(question or "")
    # 'SF' only after a number or 'in / many / of' ('6,095 SF', 'how many SF'): a room code 'SF-01' is not a unit
    if re.search(r"(?i)\b(square\s+f(?:ee|oo)t|sq\.?\s*f(?:ee)?t\.?|ft2)\b|ft²|(?:\d|\b(?:in|many|of))\s*sf\b", q):
        return "ft2"
    if re.search(r"(?i)\b(square\s+met(?:re|er)s?|sq\.?\s*m\b|m2)\b|m²", q):
        return "m2"
    if re.search(r"(?i)\b(?:in|how\s+many)\s+(?:linear\s+|running\s+)?(?:feet|foot|ft)\b|\blinear\s+feet\b", q):
        return "ft"
    if re.search(r"(?i)\b(?:in|how\s+many)\s+(?:linear\s+|running\s+)?met(?:re|er)s\b", q):
        return "m"
    return None


def _conv(v, frm, to):
    """v from unit frm to unit to (m² <-> ft², m <-> ft); the same value when the units agree; None = no known factor."""
    a, b = _unit_key(frm), _unit_key(to)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if a == b:
        return v
    k = _UNIT_K.get((a, b))
    return v * k if k else None


# a thing IN a room, not the room: 'the kitchen door', 'the windows in the kitchen', 'the kitchen counter' (a room's
# kept area / outline never answers them; 'the kitchen floor' / 'carpet area of the kitchen' are the room)
_THING = (r"(?:doors?|door\s*ways?|windows?|walls?|openings?|counters?|counter\s*tops?|worktops?|benches|bench|cabinets?|"
          r"cupboards?|wardrobes?|closets?|islands?|sinks?|baths?|tubs?|showers?|stairs?|stair\s*ways?|steps?|columns?|"
          r"pillars?|beams?|tables?|beds?|desks?|radiators?|fireplaces?|chimneys?|ducts?|pipes?|fixtures?|lights?|"
          r"outlets?|sockets?|tiles?|rugs?|appliances?|equipment|furniture|ceiling\s+fans?|vents?)")


# a subject that is a quantity, not a place ('gross floor area' from 'the gross floor area of the ground floor'): it never
# matches another question by itself (the same question still does)
_QTY_SUBJ = re.compile(r"(?i)\b(?:areas?|gross|net|total|sum|usable|perimeter|length|width|size|volume)\b")


def _about_room(subject: str, question: str, task: str | None) -> bool:
    """The question is about the room `subject` itself (an area / length of the room, or a count in it)."""
    s = re.escape(subject)
    if not re.search(rf"(?i)\b{s}\b", question) or _QTY_SUBJ.search(subject):
        return False
    # the question names another room: 'the kitchen on Level 01' is not Level 01 (a kept storey total)
    ph = _room_phrase(question)
    if ph and ph.lower() != subject.lower() and subject.lower() not in ph.lower():
        return False
    if task == "count":  # 'how many doors in the kitchen': the counted thing is checked by the caller (same target)
        return True
    # 'skirting around the walls of the main hall' is the room's own outline
    question = re.sub(r"(?i)\b(?:a?round|along)\s+(?:all\s+)?(?:the\s+)?(?:walls?|edges?)\s+(?:of|in)\s+", "of ", question)
    return not (re.search(rf"(?i)\b{s}(?:'s)?\s+{_THING}\b", question)
                or re.search(rf"(?i)\b{_THING}\s+(?:of|in|into|to|from|for|inside|at|on|along)\s+"
                             rf"(?:the\s+|this\s+|that\s+|our\s+|my\s+)?{s}\b", question))


def _page_ok(i: dict, page) -> bool:
    """A value kept on one page of a multi-page upload answers that page only ('the kitchen' on Level 00 is not Level
    01's); unknown pages (older items, requests without a page) match as before."""
    try:
        return page is None or i.get("page") is None or int(i["page"]) == int(page)
    except (TypeError, ValueError):
        return True


def _value_hit(fid, question, items, page=None) -> dict | None:
    """The visitor's own value for this question: the same question, or the same task about the same named room
    ('How big is the kitchen?' after 'What is the floor area of the kitchen?'); never totals / several rooms, never a
    thing in that room ('the kitchen door'), and a count only for the same counted thing (doors are not windows)."""
    qn = _norm_q(question)
    task = _task_of(question)
    hit = [i for i in items if i["kind"] == "value" and i["file"] == fid and _page_ok(i, page) and (
        i.get("question_norm") == qn
        or (i.get("subject") and i.get("task") and i["task"] == task
            and _about_room(i["subject"], question, task)
            and (task != "count" or (_target_of(question) or "?") == (_target_of(i.get("question") or "") or "!"))
            and not re.search(r"(?i)\b(total|sum|all|each|and)\b", question)
            # a kept LENGTH answers the same kind of length only: 'how long' is not 'how wide' / skirting / perimeter
            and (task != "length" or _length_kind(i.get("question") or "") == _length_kind(question))))]
    return hit[-1] if hit else None


def _length_kind(question: str) -> str:
    """Which length a question asks for: skirting (net / gross), perimeter, wide, height, long; 'length' = unknown."""
    q = str(question or "")
    if _BASE_Q.search(q):
        return "skirting-gross" if _GROSS_Q.search(q) else "skirting"
    if _PERIM_Q.search(q):
        return "perimeter"
    if _WIDE_Q.search(q):
        return "wide"
    if re.search(r"(?i)\b(?:height|high|tall)\b", q):
        return "height"
    if _LONG_Q.search(q):
        return "long"
    return "length"


def kept_answer(fid: str, question: str, ctx: dict) -> dict | None:
    """AUDIT RECHECK-0026 (captain 00:31): a value the visitor KEPT (their measurement / their number) answers its
    question by itself — the drawing is not read again (the demo waited 46 s for Gemini, then showed the kept 29.99 m²
    anyway). Only when the kept value is the whole answer: same question or same task + room; lengths derived from a
    measured outline still run the readers (their ±2 % 'confirmed' check needs our reading). None = ask as usual."""
    try:
        if not ctx or not ctx.get("on"):
            return None
        it = _value_hit(fid, question, ctx.get("items") or [], page=ctx.get("page"))
        if it is None or it.get("value") is None:
            return None
        out = {"value": None, "unit": it.get("unit"), "confidence": 0.0, "evidence": [], "solver": "corrections/kept",
               "trace": ["kept: the visitor's own answer to this question (Learned); the drawing was not read again",
                         "data_used: local"],
               "assumptions": [], "missing": [], "data_used": ["local"],
               "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0, "wall_seconds": 0.0}}
        try:
            from stoa.router.intent import parse

            out["intent"] = parse(question).to_dict()
        except Exception:  # noqa: BLE001 (the card falls back to its own wording)
            pass
        return out
    except Exception:  # noqa: BLE001 — never block a question: ask as usual
        return None


def _apply_values(fid, question, out, items, learned, page=None):
    it = _value_hit(fid, question, items, page=page)
    if it is None:
        return out
    ours = out.get("value")
    unit = it.get("unit") or out.get("unit")
    value = it["value"]
    # the question names another unit than the one it was kept in ('… in square feet?' after a m² measurement): the same
    # number converted, and said ('= your 16.2 m²'); our reading is compared in that unit too
    was = ""
    want = _asked_unit(question)
    if want and it.get("unit") and _unit_key(want) != _unit_key(it["unit"]) and _conv(value, it["unit"], want) is not None:
        was = f" (= your {value} {_SHOWN.get(_unit_key(it['unit']), it['unit'])})"
        value, unit = round(_conv(value, it["unit"], want), 2), want
    if ours is not None and out.get("unit") and _unit_key(out.get("unit")) != _unit_key(unit):
        c = _conv(ours, out.get("unit"), unit)
        ours = round(c, 2) if c is not None else ours
    it = {**it, "value": value, "unit": unit}
    out["value"], out["unit"] = value, unit
    # the visitor's own number is 'very sure'; OUR traced outline they accepted unchecked is a best estimate (audit 00:26:
    # 'Fairly sure' on a 23 %-off Suggest trace; the EFA scan's Suggest gives the 58 m² strip of a 180 m² hall)
    out["confidence"] = 0.5 if it.get("traced") else max(float(out.get("confidence") or 0), 0.95)
    out.setdefault("evidence", [])
    # a hand measurement (W5's measure tool: the outline / line the user drew) is the evidence of their number; ours
    # stays listed after it, marked as our reading, so both can be compared on the drawing
    mine = [dict(e) for e in it.get("evidence") or [] if isinstance(e, dict)]
    shown_unit = _SHOWN.get(_unit_key(it.get("unit")), it.get("unit") or "")
    for e in mine:
        e.setdefault("file", fid)
        # '<Room>: <n> m² …' is how the card names the room it shows ('floor area of the kitchen', customer.ts noteRoom)
        by = "our traced outline, chosen by you" if it.get("traced") else "measured by you"
        e["note"] = e.get("note") or (f"{it['subject']}: {it['value']}{(' ' + shown_unit) if shown_unit else ''} {by}"
                                      if it.get("subject") else
                                      f"{by}: {it['value']}{(' ' + shown_unit) if shown_unit else ''}")
        e.setdefault("exact", False)
    agrees = _close(ours, it["value"])
    if mine and not agrees:
        # ours stays listed after it as 'our reading'; its '<n> m²' is written 'sq m' so the card does not count two
        # outlines as two rooms ('floor area of 2 kitchens together', customer.ts heroSentence counts '<n> m²' notes)
        old_ev = [{**e, "note": _ours_note(ours, e.get("note"))}
                  for e in out["evidence"]]
        out["evidence"] = [*mine, *old_ev]
    elif mine and not out["evidence"]:
        out["evidence"] = mine
    # F1 01:12: Measure 'Suggest' -> 'Use it' is OUR trace the visitor accepted, not their measurement (audit 00:26)
    how = ("you chose our traced outline:" if it.get("traced") else
           "you measured" if it.get("source") == "measure" or mine else "you gave")
    if agrees:  # their measurement says the same as ours: a confirmation, not a change
        learned.append({"id": it["id"], "kind": "value", "changed": False, "confirmed": True,
                        "text": f"{how} the same {it['value']}{(' ' + shown_unit) if shown_unit else ''}{was}"
                                + (f" for the {it['subject']}" if it.get("subject") else "")})
        return out
    learned.append({"id": it["id"], "kind": "value", "changed": ours != it["value"],
                    "text": f"{how} {it['value']}{(' ' + shown_unit) if shown_unit else ''}{was}"
                            + (f" for the {it['subject']}" if it.get("subject") else " for this question")
                            + (f" (we read {ours})" if ours not in (None, it["value"]) else "")
                            + (f": {it['note']}" if it.get("note") else "")})
    return out


# 'wall base' / 'cove base' = US sheets' word for skirting (Stoa's AFCEC questions: 'linear feet of wall base')
_BASE_Q = re.compile(r"(?i)\b(?:skirting|baseboards?|base\s*boards?|(?:wall|cove|rubber|vinyl)\s+base\b|base\s+mou?lding)")
# 'do not subtract door openings', 'including the doors', 'gross': the whole outline; 'door openings excluded' = subtract
_GROSS_Q = re.compile(r"(?i)\b(?:do\s+not|don'?t|without|not)\s+(?:subtract|deduct|exclud|remov|tak)\w*|"
                      r"\b(?:includ\w*|ignor\w*|regardless\s+of|counting)\s+(?:the\s+|any\s+)?(?:doors?|door\s*ways?|openings?)|\bgross\b")
_PERIM_Q = re.compile(r"(?i)\bperimeter\b|\b(?:around|round)\s+the\s+(?:walls|outside|edge)")
_LONG_Q = re.compile(r"(?i)\bhow\s+long\b|\blength\b|\blong(?:er|est)?\b")
_WIDE_Q = re.compile(r"(?i)\bhow\s+wide\b|\bwidth\b|\bwide\b|\bbreadth\b|\bacross\b")


def _outline_of(it: dict) -> tuple[list[list[float]], int, str] | None:
    for e in it.get("evidence") or []:
        poly = e.get("polygon") if isinstance(e, dict) else None
        if isinstance(poly, list) and len(poly) >= 3 and all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in poly):
            space = e.get("bbox_space") or ("pdf_pt" if str(it.get("file") or "").lower().endswith(".pdf") else "image_px")
            return [[float(p[0]), float(p[1])] for p in poly], int(e.get("page") or 0), str(space)
    return None


def _shoelace(poly) -> float:
    return abs(sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly)))) / 2


def _outline_lengths(poly) -> tuple[float, float, float]:
    """(perimeter, long side, short side) of an outline in its own units; the sides of its smallest enclosing rectangle."""
    per = sum(((poly[i][0] - poly[(i + 1) % len(poly)][0]) ** 2 + (poly[i][1] - poly[(i + 1) % len(poly)][1]) ** 2) ** 0.5
              for i in range(len(poly)))
    try:
        import cv2
        import numpy as np

        (_, _), (w, h), _ = cv2.minAreaRect(np.array(poly, dtype=np.float32))
    except Exception:  # noqa: BLE001 — no OpenCV: the axis-aligned box
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
    return per, max(w, h), min(w, h)


def _apply_outline(fid, question, out, items, learned, page=None):
    """The room the visitor MEASURED by hand (an area kept from the Measure tool, with their outline) answers that room's
    later LENGTH questions too: how long / how wide (sides of the outline's smallest enclosing rectangle), perimeter,
    skirting (F1's baseboard test along the outline: perimeter minus the door openings it finds; 'do not subtract door
    openings' = the perimeter). The scale is theirs: outline units per metre = sqrt(outline area / their m²). Measured on
    X1's EFA primary scan: 'main hall' 4.78 m² (OCR read 'Maint'), length / width 10.15 (a printed grid dimension)."""
    if _task_of(question) != "length" or _SEVERAL.search(re.sub(r"(?i)\bskirting\s+and\s+\w+", "", question)):
        return out
    hit = [i for i in items if i["kind"] == "value" and i["file"] == fid and _page_ok(i, page) and i.get("task") == "area" and i.get("subject")
           and _unit_key(i.get("unit") or "m2") in ("m2", "ft2") and _about_room(i["subject"], question, "length")
           and _outline_of(i)]
    if not hit:
        return out
    it = hit[-1]
    poly, page, space = _outline_of(it)
    try:
        # outline units per metre, as the visitor measured it (a ft² value: their area in m² first)
        upm = (_shoelace(poly) / _conv(it["value"], it.get("unit") or "m2", "m2")) ** 0.5
    except (TypeError, ValueError, ZeroDivisionError):
        return out
    if not upm > 0:
        return out
    # the length unit: the one the question names, else feet on a ft² measurement / a reading in feet, else metres
    asked = _asked_unit(question)
    lu = asked if asked in ("m", "ft") else (
        "ft" if _unit_key(it.get("unit")) == "ft2" or _unit_key(out.get("unit")) == "ft" else "m")

    def L(v_m: float) -> float:  # metres -> the answer's length unit
        return round(_conv(v_m, "m", lu), 2)

    def Ls(v_m: float) -> str:  # the same, written with 2 decimals
        return f"{_conv(v_m, 'm', lu):.2f}"

    au = _SHOWN.get(_unit_key(it.get("unit") or "m2"), "m²")
    per, long_u, short_u = _outline_lengths(poly)
    base, gross = bool(_BASE_Q.search(question)), bool(_GROSS_Q.search(question))
    ev_extra, conf, card = [], 0.85, None
    if base and not gross:
        try:  # F1's baseboard test along the outline (wall line just outside each stretch?) at the visitor's scale
            from stoa.features import region as R

            reg = R.Region(page=page, space=space, polygon=poly, snap=False)
            m_per_u, _why = R._outline_scale(_abs(fid), reg, None if space == "pdf_pt" else upm)
            # F1's test is asked in ITS words: it does not know 'wall base' (US) and answered that question with the
            # outline's AREA (11.6 m² taken as 11.6 m of skirting on AFCEC A-101); we already know it is skirting, net
            a = R.outline_measure(_abs(fid), "How long is the skirting of this room, door openings excluded?", reg,
                                  user_px_per_m=None if space == "pdf_pt" else upm)
            d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
        except Exception:  # noqa: BLE001 — no wall test: leave the answer as it was
            return out
        if d.get("value") is None or _unit_key(d.get("unit")) not in ("m", "ft") or not m_per_u or any(
                "nothing deducted" in str(m) or "could not test" in str(m) for m in d.get("missing") or []):
            return out
        k = (1 / upm) / m_per_u  # their scale over the one the wall test used (1.0 on pictures)
        val = _conv(float(d["value"]), _unit_key(d.get("unit")), "m") * k
        opens = [e for e in d.get("evidence") or [] if "opening" in str(e.get("note") or "")]
        ev_extra = [{**e, "file": fid} for e in opens]
        def how():
            return (f"the perimeter of your outline {Ls(per / upm)} {lu} minus {len(opens)} door opening"
                    f"{'' if len(opens) == 1 else 's'} found along it")

        def card():  # the card's skirting sentence reads 'Room: skirting X m = outline Y m minus k openings' (customer.ts)
            return (f"skirting {L(val_m)} {lu} = outline {Ls(per / upm)} {lu} minus {len(opens)} opening"
                    f"{'' if len(opens) == 1 else 's'}")
        conf = min(0.75, float(d.get("confidence") or 0.7))
    elif base or _PERIM_Q.search(question):
        val = per / upm

        def how():
            return "the perimeter of your outline" + (" (door openings not subtracted, as asked)" if base else "")
    elif _WIDE_Q.search(question):
        val = short_u / upm

        def how():
            return f"the short side of your outline ({Ls(long_u / upm)} x {Ls(short_u / upm)} {lu})"
    elif _LONG_Q.search(question):
        val = long_u / upm

        def how():
            return f"the long side of your outline ({Ls(long_u / upm)} x {Ls(short_u / upm)} {lu})"
    else:
        return out
    val_m = val
    ours = out.get("value")
    ou = _unit_key(out.get("unit"))
    if ours is not None and ou in ("m", "ft") and _close(_conv(ours, ou, "m"), val_m, 0.02):
        # the reading says the same (within 2 %): keep it, confirmed by their outline, said in the reading's unit
        lu = ou
        # skirting with the openings taken off: both readings find openings the same way (A-101 Break Room: 40.58 and
        # 40.78 ft both miss the closet's sliding doors, gold 34.65), so agreeing lifts it to 'Best estimate' only
        out["confidence"] = max(float(out.get("confidence") or 0), 0.55 if base and not gross else 0.75)
        learned.append({"id": it["id"], "kind": "value", "changed": False, "confirmed": True,
                        "text": f"the {it['subject']} {_whose(it)} agrees ({how()}: {L(val_m)} {lu})"})
        return out
    val = L(val_m)
    how, card = how(), (card() if card else None)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    mine = {"file": fid, "page": page, "bbox": [min(xs), min(ys), max(xs), max(ys)], "bbox_space": space,
            "polygon": poly, "exact": False,
            "note": f"{it['subject']}: {card} (your {it['value']} {au} outline)" if card else
                    f"{it['subject']}: {val} {lu} = {how} (your {it['value']} {au} outline)"}
    old_ev = [{**e, "note": _ours_note(ours, e.get("note"))}
              for e in out.get("evidence") or []] if ours is not None else []
    out["value"], out["unit"], out["confidence"] = val, lu, max(conf, min(float(out.get("confidence") or 0), conf))
    out["evidence"] = [mine, *ev_extra, *old_ev]
    out["trace"] = [*(out.get("trace") or []), f"learn: outline: {how} = {val} {lu} ({val_m:.3f} m) at {upm:.2f} units "
                                               f"per m (your {it['value']} {au} over the outline's {_shoelace(poly):.0f} units²)"]
    learned.append({"id": it["id"], "kind": "value", "changed": True,
                    "text": f"from the {it['subject']} {_whose(it)} ({it['value']} {au}): {how}"
                            + (f" (we read {ours})" if ours not in (None, val) else "")})
    return out


def _whose(it: dict) -> str:
    """'you measured' (their outline) or 'you chose (our traced outline)' (Measure 'Suggest' -> 'Use it', F1 01:12)."""
    return "you chose (our traced outline)" if it.get("traced") else "you measured"


def _ours_note(ours, note) -> str:
    """Our reading, listed after the visitor's: '<n> m²' written 'sq m' (the card counts '<n> m²' notes as rooms:
    'floor area of 2 kitchens together', customer.ts heroSentence) and 'Room: skirting …' written 'Room, skirting …' (the
    card's skirting sentence reads the first 'Room: skirting X m = outline Y m minus k openings' note: cycle 7's walk
    showed 'its outline 8.67 m minus 1 opening' under the visitor's 47.51 m)."""
    t = re.sub(r"(\d)\s*(?:m²|m2)(?!\w)", r"\1 sq m", f"our reading ({ours}): {note or ''}".rstrip(": "))
    return re.sub(r"(?i):\s*(skirting|baseboard)\b", r", \1", t)


def _close(a, b, rel: float = 0.005) -> bool:
    """Two numbers that say the same (within 0.5 %)."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-9)


def _target_of(question: str) -> str | None:
    for lab, pat in _WORDS.items():
        if re.search(rf"(?i)\b(?:{pat})\b", question):
            return lab
    try:
        from stoa.router.intent import parse

        t = parse(question)
        return t.target if t.task == "count" else None
    except Exception:  # noqa: BLE001
        return None


def _apply_symbols(fid, question, out, items, learned):
    syms = [i for i in items if i["kind"] == "symbol" and i["file"] == fid]
    # an accepted 'N more like it' on ANOTHER sheet of the set that prints the same scale: its mark is matched here too
    syms += [i for i in items if i["kind"] == "symbol" and i["file"] != fid and i.get("bboxes")
             and i.get("bbox_space") in (None, "pdf_pt") and _same_print(i["file"], fid)]
    if not syms or not re.search(r"(?i)\b(how many|number of|count|anzahl|wie viele|kuinka monta|combien)\b", question):
        return out
    target = _target_of(question)
    v = out.get("value")
    if not target or not isinstance(v, (int, float)) or isinstance(v, bool):
        return out
    mods = set(re.findall(r"(?i)\b(double|single|sliding|fire|external|internal|exterior|interior|revolving)\b", question))
    ev = list(out.get("evidence") or [])
    boxes = [e for e in ev if e.get("bbox") and (e.get("file") in (None, "", fid) or Path(str(e.get("file"))).name == Path(fid).name)]
    # boxes that show WHERE the answer looked (a view 'view: 1.krs', a room, the searched area) are no counted symbol:
    # a rejected door inside a view took the view out too (16 -> 14 for one click, walk c10), and a door the visitor
    # marked inside one was 'already counted'. They still bound where the answer counted (_inside_scope)
    regions = {id(e) for e in boxes if _region_box(e, boxes)}
    added = removed = liked = unliked = gone_leaves = 0
    used = []
    mine: list = []
    for s in syms:
        lab = s["label"]
        if not (lab == target or lab.endswith(" " + target) or lab.rstrip("s") == target):
            continue
        # 'this is a door' adds to 'how many double doors?' only when it says 'double door'; 'this is not a door' takes
        # the box out of every door count, 'not a double door' only out of double-door counts
        lab_mods = set(re.findall(r"(?i)\b(double|single|sliding|fire|external|internal|exterior|interior|revolving)\b", lab))
        if s["is"] and mods and not all(m.lower() in lab for m in mods):
            continue
        if not s["is"] and lab_mods and not lab_mods <= {m.lower() for m in mods}:
            continue
        page = s.get("page")
        same_all = [e for e in boxes if (page is None or e.get("page") in (None, page))
                    and (not s.get("bbox_space") or not e.get("bbox_space") or e.get("bbox_space") == s.get("bbox_space"))]
        same = [e for e in same_all if id(e) not in regions]
        if not s["is"] and s["file"] != fid:  # a rejected group from another sheet: its look-alikes among OUR boxes
            over = _cross_alike(s, fid, same)
        else:  # the rejected box, and the counted boxes the user agreed look like it ('N more like it', twin)
            group = s.get("bboxes") if not s["is"] and s.get("bboxes") else [s["bbox"]]
            over = [e for e in same if any(_overlaps(e["bbox"], b) for b in group)]
        if s["is"]:
            # one mark, or a mark + the look-alikes the user accepted ('N more like it'): each counts once, where the
            # answer counted, and never on a box already counted (by the reader or by another of the user's marks)
            group = s.get("bboxes") or [s["bbox"]]
            cross = s["file"] != fid
            if cross:  # the look-alikes of that mark on THIS sheet (page of the answer's boxes), never the counted ones
                tpage = next((e.get("page") for e in boxes if e.get("page") is not None), 0)
                same_all = [e for e in boxes if e.get("page") in (None, tpage)]
                same = [e for e in same_all if id(e) not in regions]
                group = [None] + _cross_hits(s, fid, tpage, [e["bbox"] for e in same])
            n0 = added
            for j, bb in enumerate(group):
                if bb is None:
                    continue
                if any(_overlaps(e["bbox"], bb) for e in same) or any(_overlaps(b, bb) for b in mine):
                    continue
                if not _inside_scope(bb, same_all):
                    continue
                box = list(bb) if s.get("bboxes") else _sized(bb, same)
                ev.append({"file": fid, "page": tpage if cross else page, "bbox": box, "bbox_space": s.get("bbox_space") or
                           (same[0].get("bbox_space") if same else None),
                           "note": (f"{lab} — like the one you marked on {_sheet_name(s['file'])}" if cross
                                    else f"{lab} — marked by you" if j == 0 else f"{lab} — like the one you marked"),
                           "exact": True})
                mine.append(box)
                added += 1
                liked += j > 0
            if added > n0:
                used.append(s)
        elif not s["is"] and over:
            for e in over:
                ev.remove(e)
                boxes.remove(e)
                gone_leaves += 2 if re.search(r"(?i)\(2 leaves\)|\bdouble\b|\bpair\b", str(e.get("note") or "")) else 1
            removed += len(over)
            unliked += len(over) - 1 if s.get("bboxes") and s["file"] == fid else len(over) if s["file"] != fid else 0
            used.append(s)
    if not used:
        return out
    out["value"] = v + added - removed
    out["evidence"] = ev
    # the reader's own 'N doors (M leaves)' line (the card's Scope reads it) says the corrected count too
    pat = re.compile(rf"\b{int(v)} {target}s? \((\d+) leaves?\)") if float(v).is_integer() else None
    pat2 = re.compile(rf"\b{int(v)} {target}s?, (\d+) lea(?:ves|f)\b") if pat else None  # 'Counted door SETS …: 115 doors, 144 leaves'
    n = out["value"]
    for key in ("assumptions", "trace"):
        if pat and out.get(key):
            out[key] = [pat2.sub(lambda m: f"{n} {target}{'s' if n != 1 else ''}, {max(0, int(m.group(1)) - gone_leaves + added)} leaves",
                                 pat.sub(lambda m: f"{n} {target}{'s' if n != 1 else ''} "
                                                   f"({max(0, int(m.group(1)) - gone_leaves + added)} leaves)", str(x)))
                        for x in out[key]]
    bits = ([f"+{added} {target}{'s' if added != 1 else ''} you marked" + (f" ({liked} found like yours)" if liked else "")
             if added > liked else f"+{added} {target}{'s' if added != 1 else ''} like the one you marked"
             + (f" on {_sheet_name(used[0]['file'])}" if used and used[0]['file'] != fid else "")]
            if added else []) + (
        [f"−{removed} you said {'are' if removed != 1 else 'is'} not a {target}" + (f" ({unliked} found like yours)"
          if 0 < unliked < removed else "") if unliked < removed
         else f"−{removed} like the one you said is not a {target}"
         + (f" on {_sheet_name(used[0]['file'])}" if used and used[0]['file'] != fid else "")] if removed else [])
    for s in used:
        learned.append({"id": s["id"], "kind": "symbol", "changed": True,
                        "text": f"{', '.join(bits)} (we counted {v})"})
        bits = []  # the sentence once; the other items only count as applied
    learned[:] = [x for x in learned if x.get("text")]
    return out


_CROSS: dict[tuple, list] = {}


def _cross_alike(item: dict, fid: str, same: list) -> list:
    """A group the user rejected on another sheet of the set (same print): the counted boxes HERE that look like its
    first box (alike() with that sheet's crop), as evidence entries of ``same``. Cached per (item, sheet, boxes)."""
    key = ("not", item["id"], fid, tuple(tuple(e["bbox"]) for e in same))
    if key not in _CROSS:
        try:
            page = next((e.get("page") for e in same if e.get("page") is not None), 0)
            r = alike(fid, page, item["bbox"], "pdf_pt", [e["bbox"] for e in same],
                      source={"file": item["file"], "page": item.get("page") or 0, "bbox": item["bbox"]})
            _CROSS[key] = [] if r.get("reason") else r["boxes"]
        except Exception:  # noqa: BLE001
            _CROSS[key] = []
    hits = _CROSS[key]
    return [e for e in same if any(list(map(float, e["bbox"])) == list(b) for b in hits)]


def _sheet_name(fid: str) -> str:
    """A sheet's readable name from its file name: 'SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf' ->
    'Special School Plan Level 00' (the longest run of words joined by '_'), else the file name."""
    stem = Path(str(fid)).stem
    runs = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", stem)
    best = max(runs, key=len) if runs else ""
    return best.replace("_", " ") if len(best) >= 8 else Path(str(fid)).name


def _same_print(a: str, b: str) -> bool:
    """Two sheets whose symbols are drawn at the same size: both PDFs printing the same scale (DfE L00 / L01: 1:200)."""
    try:
        if not (a.lower().endswith(".pdf") and b.lower().endswith(".pdf")):
            return False
        pa, pb = _printed_scale(a), _printed_scale(b)
        return bool(pa) and pa == pb
    except Exception:  # noqa: BLE001
        return False


def _cross_hits(item: dict, fid: str, page: int, counted: list) -> list:
    """The look-alikes of an accepted mark from another sheet on this page (cached per item, sheet, page)."""
    key = (item["id"], fid, page, len(counted))
    if key not in _CROSS:
        try:
            r = similar(fid, page, item["bbox"], "pdf_pt", exclude=counted,
                        source={"file": item["file"], "page": item.get("page") or 0, "bbox": item["bbox"]})
            _CROSS[key] = [] if r.get("reason") else r["boxes"]
        except Exception:  # noqa: BLE001
            _CROSS[key] = []
    return _CROSS[key]


def _sized(bb, same) -> list:
    """The user's mark drawn at the size of the symbols the reader boxed on this page (the median box), centred where
    they clicked: the plugin only knows the click (a fixed box of about half a door), the reader knows the door size."""
    try:
        ws = sorted(float(e["bbox"][2]) - float(e["bbox"][0]) for e in same)
        hs = sorted(float(e["bbox"][3]) - float(e["bbox"][1]) for e in same)
        if len(ws) < 2:
            return list(bb)
        w, h = ws[len(ws) // 2], hs[len(hs) // 2]
        cw, ch = float(bb[2]) - float(bb[0]), float(bb[3]) - float(bb[1])
        if not (0 < w <= 8 * cw and 0 < h <= 8 * ch):  # a reader's box that is a whole room / view: keep the click box
            return list(bb)
        cx, cy = (float(bb[0]) + float(bb[2])) / 2, (float(bb[1]) + float(bb[3])) / 2
        return [round(cx - w / 2, 2), round(cy - h / 2, 2), round(cx + w / 2, 2), round(cy + h / 2, 2)]
    except Exception:  # noqa: BLE001
        return list(bb)


_REGION_NOTE = re.compile(r"(?i)\s*(view|sheet|plan|drawing|region|scope|searched|looked|room outline)\b")


def _region_box(e: dict, boxes: list[dict]) -> bool:
    """A box that shows where the answer looked, not a counted symbol: its note says so ('view: 1.krs', 'searched …'),
    or it is > 25 x the (lower) median box of the answer (a room's or a view's outline among door marks)."""
    if _REGION_NOTE.match(str(e.get("note") or "")):
        return True
    try:
        area = lambda b: max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))  # noqa: E731
        areas = sorted(area(x["bbox"]) for x in boxes)
        ref = areas[(len(areas) - 1) // 2]
        return len(areas) >= 2 and ref > 0 and area(e["bbox"]) > 25 * ref
    except (TypeError, IndexError, ValueError, KeyError):
        return False


def _inside_scope(bb, same) -> bool:
    """A symbol the user marked counts for this answer when it lies where the answer counted: the extent of its boxes
    (+15 %), or anywhere when the answer counted the whole sheet (>= 5 boxes) or showed no boxes at all."""
    if len(same) >= 5 or not same:
        return True
    x0 = min(e["bbox"][0] for e in same)
    y0 = min(e["bbox"][1] for e in same)
    x1 = max(e["bbox"][2] for e in same)
    y1 = max(e["bbox"][3] for e in same)
    dx, dy = (x1 - x0) * 0.15 + 1, (y1 - y0) * 0.15 + 1
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return x0 - dx <= cx <= x1 + dx and y0 - dy <= cy <= y1 + dy


# -- 'N more like it': one symbol the user confirmed, matched over the page ---------------------------------------------
SIMILAR_DPI = 150  # PDF pages rendered at 150 dpi; pictures used as they are up to SIMILAR_MAX_PX on the long side
SIMILAR_MAX_PX = 4000
SIMILAR_THR = 0.75  # experiments/f7/symbol_match.py: 0.75 -> precision 0.99-1.0 on 5/5 door marks; 0.6 floods
SIMILAR_CAP = 300  # more hits than this: the mark is too generic (a wall corner, a hatch): offer nothing


def _page_gray(fid: str, page: int, space: str | None):
    """(grey page image, drawing units -> image px) for the file's page in its evidence space: PDF points (y down, the
    unrotated page) or the uploaded picture's pixels (big pictures shrunk)."""
    import numpy as np

    p = _abs(fid)
    if p.lower().endswith(".pdf") and space != "image_px":
        import pymupdf

        with pymupdf.open(p) as d:
            pg = d[int(page or 0)]
            if pg.rotation:
                pg.set_rotation(0)  # evidence boxes are in the unrotated page's points (in memory only)
            pix = pg.get_pixmap(dpi=SIMILAR_DPI, colorspace=pymupdf.csGRAY)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return img, SIMILAR_DPI / 72
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(p) as im:
        im.seek(int(page or 0)) if getattr(im, "n_frames", 1) > 1 else None
        g = im.convert("L")
        k = min(1.0, SIMILAR_MAX_PX / max(g.size))
        if k < 1:
            g = g.resize((max(1, round(g.width * k)), max(1, round(g.height * k))), Image.LANCZOS)
        return np.asarray(g).copy(), k


def _ink(img):
    """Ink = high, paper = 0: a scan's grey, noisy paper is taken off (its median level + a little noise margin) and the
    darkest ink stretched to 255, so the thresholds below mean the same on a vector render and on a scan / photo."""
    import numpy as np
    import cv2

    inv = 255 - img
    paper = float(np.median(inv))
    if paper < 3:  # a clean render: as it is
        return inv
    inv = cv2.medianBlur(inv, 3)  # speckle
    top = float(np.percentile(inv, 99.7))
    k = 255.0 / max(top - paper - 12, 1.0)
    return np.clip((inv.astype(np.float32) - paper - 12) * k, 0, 255).astype(np.uint8)


def _snap(inv, x0: int, y0: int, x1: int, y1: int, grow: int = 5) -> tuple[int, int, int, int] | None:
    """Fit the mark (a box around the user's click, at the reader's symbol size) to the symbol under it: the thin-line
    component (a door's arc + leaf; walls removed as the dark ink that survives a 4 px erosion at 150 dpi) that the box
    touches, of about a symbol's size, cut tight like the reader's own boxes. A loose box takes in the wall, and a wall
    matches every wall; a tight one matched the reader's doors at precision 0.99 (experiments/f7/symbol_match.py)."""
    import numpy as np
    import cv2

    w, h = x1 - x0, y1 - y0
    H, W = inv.shape
    ax0, ay0 = max(0, x0 - w), max(0, y0 - h)
    ax1, ay1 = min(W, x1 + w), min(H, y1 + h)
    patch = inv[ay0:ay1, ax0:ax1]
    ink = (patch > 40).astype(np.uint8)  # hairlines render light grey at 150 dpi: a low threshold keeps an arc whole
    if not ink.any():
        return None
    solid = cv2.dilate(cv2.erode((patch > 128).astype(np.uint8), np.ones((4, 4), np.uint8)), np.ones((grow, grow), np.uint8))
    thin = ink & (1 - solid)
    n, lab, st, _ = cv2.connectedComponentsWithStats(thin, connectivity=8)
    side = max(w, h)
    best, best_key = None, None
    for i in range(1, n):
        cx0, cy0, cw, ch, area = (int(v) for v in st[i])
        gx0, gy0 = cx0 + ax0, cy0 + ay0
        if not (gx0 < x1 and gx0 + cw > x0 and gy0 < y1 and gy0 + ch > y0):  # must touch the click box
            continue
        if not (0.35 * side <= max(cw, ch) <= 2.5 * side) or area < 0.5 * side:
            continue
        key = (area, -abs(max(cw, ch) - side))
        if best_key is None or key > best_key:
            best, best_key = (gx0, gy0, gx0 + cw, gy0 + ch), key
    if best is None:  # no thin symbol of about a symbol's size touches the click
        # a leaf can join its neighbour through the wall's anti-aliased outline (a row of cupboards): take more of the
        # wall away once (the 5 px path is the measured one: 17/17 on DfE L00; this only rescues clicks it gave up on)
        return _snap(inv, x0, y0, x1, y1, 9) if grow == 5 else None
    bx0, by0, bx1, by1 = best
    return max(0, bx0 - 1), max(0, by0 - 1), min(W, bx1 + 1), min(H, by1 + 1)


def similar(path: str, page: int | None, bbox, bbox_space: str | None = None, exclude=None, thr: float = SIMILAR_THR,
            cap: int = SIMILAR_CAP, source: dict | None = None, pictures: bool = False) -> dict:
    """The symbols on the page that look like the one the user marked at ``bbox`` (template matching, no model): the
    mark at the size of the symbols the reader boxed (``exclude``: the answer's own boxes, already counted), matched in
    its 8 orientations (4 rotations x mirror), TM_CCOEFF_NORMED >= thr, one hit per symbol. Hits on a counted box or on
    the mark itself are left out (``counted``: how many fell on counted boxes: a sanity check that the mark looks like
    what the reader counts). Returns {boxes: [[x0, y0, x1, y1] in bbox_space], mark, counted, scores, secs, reason}.
    ``source`` = {file, page, bbox} of a mark already cut on ANOTHER sheet of the set (an accepted 'N more like it'):
    that mark is matched on this page instead (same render scale; the caller checks the sheets print the same scale)."""
    import numpy as np
    import cv2

    t0 = time.time()
    fid = file_id(path)
    ex = [list(map(float, b)) for b in (exclude or []) if b and len(b) == 4]
    mark = _sized(list(map(float, bbox)), [{"bbox": b} for b in ex])
    out = {"boxes": [], "scores": [], "mark": mark, "counted": 0, "thr": thr, "secs": 0.0, "reason": None}
    if (not fid.lower().endswith(".pdf") or bbox_space == "image_px") and not pictures:
        # pictures: Stoa's DfE PNG gave 41-75 look-alikes depending on a 14-px click shift, X1's scan none: PDF only
        out["reason"] = "on pictures this is not reliable yet (PDF drawings only)"
        return out
    img, k = _page_gray(fid, page or 0, bbox_space)
    inv = _ink(img)
    if source:
        simg, sk = _page_gray(file_id(source["file"]), source.get("page") or 0, bbox_space)
        sb = list(map(float, source["bbox"]))
        if abs(sk - k) > 1e-6:
            out["reason"] = "the two sheets are rendered at different scales"
            return out
        tpl = _ink(simg)[max(0, int(round(sb[1] * k))):int(round(sb[3] * k)), max(0, int(round(sb[0] * k))):int(round(sb[2] * k))]
        mark = [-1e9, -1e9, -1e9 + 1, -1e9 + 1]  # nothing on this sheet is the mark itself
    else:
        x0, y0 = max(0, int(round(mark[0] * k))), max(0, int(round(mark[1] * k)))
        x1, y1 = min(img.shape[1], int(round(mark[2] * k))), min(img.shape[0], int(round(mark[3] * k)))
        if x1 - x0 < 6 or y1 - y0 < 6:
            out["reason"] = "the mark is too small to match"
            return out
        snapped = _snap(inv, x0, y0, x1, y1)
        if snapped is None:  # nothing thin under the click (a wall, a fill, a blurred scan): the loose box floods
            out["reason"] = "no single symbol found under the mark (a wall, a fill, or too blurred to cut out)"
            return out
        x0, y0, x1, y1 = snapped
        mark = [round(x0 / k, 2), round(y0 / k, 2), round(x1 / k, 2), round(y1 / k, 2)]
        out["mark"] = mark
        tpl = inv[y0:y1, x0:x1]
    if tpl.size == 0 or min(tpl.shape) < 3:
        out["reason"] = "the mark is too small to match"
        return out
    ink = float(tpl.astype(np.float32).sum())
    if tpl.std() < 8 or (tpl > 96).mean() < 0.01:
        out["reason"] = "no drawing under the mark"
        return out
    hits = []
    for m in (tpl, tpl[:, ::-1]):
        for r in range(4):
            t = np.ascontiguousarray(np.rot90(m, r))
            if t.shape[0] > inv.shape[0] or t.shape[1] > inv.shape[1]:
                continue
            res = cv2.matchTemplate(inv, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= thr)
            if len(ys) > 50 * cap:  # a flood (paper-like mark): stop early
                out["reason"] = "the mark matches too much of the drawing (a wall or hatch, not a symbol)"
                out["secs"] = round(time.time() - t0, 2)
                return out
            hits += [(float(res[y, x]), int(x), int(y), t.shape[1], t.shape[0]) for y, x in zip(ys, xs)]
    hits.sort(reverse=True)
    keep: list[tuple] = []
    for h in hits:  # one hit per symbol: centres closer than half a symbol are the same one
        cx, cy = h[1] + h[3] / 2, h[2] + h[4] / 2
        if all(abs(cx - q[1] - q[3] / 2) > q[3] / 2 or abs(cy - q[2] - q[4] / 2) > q[4] / 2 for q in keep):
            keep.append(h)
        if len(keep) > cap:
            out["reason"] = f"more than {cap} look like it: the mark is too generic to count by"
            out["secs"] = round(time.time() - t0, 2)
            return out
    for s, x, y, w, h in keep:
        b = [round(x / k, 2), round(y / k, 2), round((x + w) / k, 2), round((y + h) / k, 2)]
        if _overlaps(b, mark):
            continue
        # the correlation ignores contrast: a grey chair drawn like a door swing matches too; keep hits inked like the mark
        if not 0.5 <= float(inv[y:y + h, x:x + w].astype(np.float32).sum()) / max(ink, 1.0) <= 2.0:
            continue
        if any(_overlaps(b, c) for c in ex):
            out["counted"] += 1
            continue
        out["boxes"].append(b)
        out["scores"].append(round(s, 3))
    if out["counted"] >= 5 and len(out["boxes"]) > out["counted"]:
        # it looks like the symbols we counted AND like even more things we did not: a generic piece (a jamb, a wall
        # corner), not one symbol type (DfE L00: a click on a cupboard leaf's end -> 58 new + 12 counted, mostly jambs)
        out["reason"] = (f"it looks like {out['counted']} we counted and {len(out['boxes'])} other things: "
                         "too ambiguous to count by")
        out["boxes"], out["scores"] = [], []
    out["secs"] = round(time.time() - t0, 2)
    return out


ALIKE_INK_MAX = 1.6  # a counted box with more thin ink than this x the mark's is more than one such symbol
ALIKE_MOST = 0.5  # look-alikes > this share of the other counted boxes (>= 9 of them): offer nothing
ALIKE_THR = 0.6  # among the answer's own boxes (no flood possible): experiments/f7/alike_counted.py


def _thin(inv, grow: int = 5):
    """The thin ink only (arcs, leaves, symbol lines): the dark ink that survives a 4 px erosion (walls, fills) and its
    5 px surround taken out, as in _snap."""
    import numpy as np
    import cv2

    solid = cv2.dilate(cv2.erode((inv > 128).astype(np.uint8), np.ones((4, 4), np.uint8)), np.ones((grow, grow), np.uint8))
    return (inv * (1 - solid)).astype(np.uint8)


def alike(path: str, page: int | None, bbox, bbox_space: str | None = None, among=None, thr: float = ALIKE_THR,
          source: dict | None = None) -> dict:
    """'This is not a door' -> the OTHER boxes the answer counted that look like the one the user rejected (the twin of
    similar(): over-counting instead of under-counting). The mark is the counted box under the click as the reader cut
    it; every other counted box (``among``) is scored by the best TM_CCOEFF_NORMED of the mark inside it (+25 % margin),
    in the mark's 8 orientations, with the ink-sum ratio 0.5-2 (a grey chair is not a door). No model, ~0.1-1 s.
    ``source`` = {file, page, bbox}: a rejected mark from ANOTHER sheet that prints the same scale.
    Returns {boxes (counted boxes like it, as given), scores, mark, counted (= len(among)), secs, reason}."""
    import numpy as np
    import cv2

    t0 = time.time()
    fid = file_id(path)
    boxes = [list(map(float, b)) for b in (among or []) if b and len(b) == 4]
    mark = list(map(float, bbox))
    out = {"boxes": [], "scores": [], "mark": mark, "counted": len(boxes), "thr": thr, "secs": 0.0, "reason": None}
    # pictures too: only the answer's own boxes are scored, so a loose mark cannot flood the sheet (similar() can)
    img, k = _page_gray(fid, page or 0, bbox_space)
    inv = _thin(_ink(img))  # the reader's door boxes take in the wall jambs, and a wall matches every wall: lines only
    H, W = inv.shape
    if source:
        simg, sk = _page_gray(file_id(source["file"]), source.get("page") or 0, bbox_space)
        if abs(sk - k) > 1e-6:
            out["reason"] = "the two sheets are rendered at different scales"
            return out
        sb = list(map(float, source["bbox"]))
        tpl = _thin(_ink(simg))[max(0, int(round(sb[1] * k))):int(round(sb[3] * k)), max(0, int(round(sb[0] * k))):int(round(sb[2] * k))]
        mark = [-1e9, -1e9, -1e9 + 1, -1e9 + 1]
    else:
        tpl = inv[max(0, int(round(mark[1] * k))):min(H, int(round(mark[3] * k))),
                  max(0, int(round(mark[0] * k))):min(W, int(round(mark[2] * k)))]
    if tpl.size == 0 or min(tpl.shape) < 4 or tpl.std() < 8:
        out["reason"] = "no drawing under the mark"
        return out
    ink = float(tpl.astype(np.float32).sum())
    tpls = [np.ascontiguousarray(np.rot90(m, r)) for m in (tpl, tpl[:, ::-1]) for r in range(4)]
    for b in boxes:
        if _overlaps(b, mark):
            continue
        bw, bh = (b[2] - b[0]) * k, (b[3] - b[1]) * k
        # a box much bigger or smaller than the mark is another kind of thing (a double door vs a cupboard leaf)
        if not (0.5 <= max(bw, bh) / max(tpl.shape) <= 2.0):
            continue
        # a box holding much more line work than the mark holds more than one of it: DfE L00, a single cupboard leaf
        # 0.82-1.21 x the mark's ink vs the V-shaped folding pairs 1.87-2.39 x (the expert counts those as doors)
        own = float(inv[max(0, int(b[1] * k)):min(H, int(b[3] * k)), max(0, int(b[0] * k)):min(W, int(b[2] * k))]
                    .astype(np.float32).sum())
        if own > ALIKE_INK_MAX * ink:
            continue
        mx, my = 0.25 * bw + 3, 0.25 * bh + 3
        x0, y0 = max(0, int(b[0] * k - mx)), max(0, int(b[1] * k - my))
        x1, y1 = min(W, int(b[2] * k + mx)), min(H, int(b[3] * k + my))
        reg = inv[y0:y1, x0:x1]
        best = (-1.0, 0, 0, 0, 0)
        for t in tpls:
            if t.shape[0] > reg.shape[0] or t.shape[1] > reg.shape[1]:
                continue
            res = cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv > best[0]:
                best = (float(mv), ml[0], ml[1], t.shape[1], t.shape[0])
        s, x, y, w, h = best
        if s < thr:
            continue
        if not 0.5 <= float(reg[y:y + h, x:x + w].astype(np.float32).sum()) / max(ink, 1.0) <= 2.0:
            continue
        out["boxes"].append(b)
        out["scores"].append(round(s, 3))
    others = len(boxes) - 1
    if others >= 9 and len(out["boxes"]) > ALIKE_MOST * others:
        # most of what we counted looks like it: the visitor rejected ONE of a common symbol (a DfE L00 window looks
        # like 50-64 of the 67 counted, a plain swing door like 66 of 115); taking them all out by look is not safe
        out["reason"] = (f"it looks like most of what we counted ({len(out['boxes'])} of {others}): "
                         "too common to take out by look")
        out["boxes"], out["scores"] = [], []
    out["secs"] = round(time.time() - t0, 2)
    return out


def _apply_choice(fid, question, out, items, learned, resolve):
    cl = out.get("clarify")
    if not isinstance(cl, dict) or not cl.get("options"):
        return out
    an = _norm_q(cl.get("ask", ""))
    labels = {str(o.get("label") or "").strip().lower(): o for o in cl["options"] if isinstance(o, dict)}
    qn, task = _norm_q(question), _task_of(question)

    def same_question(i: dict) -> bool:  # a pick between readings: this question again / this room again
        if i.get("pick") != "reading":
            return True
        if i.get("asked_norm") and i["asked_norm"] == qn:
            return True
        sub = i.get("subject")
        return bool(sub and i.get("task") == task and _about_room(sub, question, task))

    picks = [i for i in items if i["kind"] == "choice" and (i["file"] == fid or i.get("scope") == "set")
             and str(i.get("label", "")).strip().lower() in labels and (not i.get("ask_norm") or i["ask_norm"] == an)
             and same_question(i)]
    if not picks:
        return out
    it = picks[-1]
    opt = labels[str(it["label"]).strip().lower()]
    ours, ours_unit = out.get("value"), out.get("unit")
    ev = list(out.get("evidence") or [])
    idx = [k for k in opt.get("evidence") or [] if isinstance(k, int) and 0 <= k < len(ev)]
    if opt.get("value") not in (None, ""):
        out["value"], out["unit"] = opt["value"], opt.get("unit") or out.get("unit")
        if idx:
            out["evidence"] = [ev[k] for k in idx]
    elif opt.get("question") and resolve is not None:
        got = resolve(opt["question"])
        if not isinstance(got, dict) or got.get("value") is None:
            return out
        for k in ("value", "unit", "confidence", "evidence", "solver", "support"):
            if k in got:
                out[k] = got[k]
        if not out.get("evidence") and idx:
            # the re-asked reader marked nothing: at least show WHICH plan the number is for (the option's own boxes)
            out["evidence"] = [{**ev[k], "note": f"{it['label']} (the plan you picked)"} for k in idx]
        elif not out.get("evidence") and isinstance(opt.get("evidence"), dict) and opt["evidence"].get("bbox"):
            # a drawing SET's choice (stoa.sheet.sets.sheet_choices) carries the sheet's own box, not indices
            out["evidence"] = [{**opt["evidence"], "note": f"{it['label']} (the sheet you picked)"}]
    else:
        return out
    out["clarify_resolved"] = {"label": it["label"], "ask": cl.get("ask"), "by": "your earlier choice", "id": it["id"],
                               "question": opt.get("question"), "was": ours, "was_unit": ours_unit,
                               "others": [str(o.get("label")) for o in cl["options"] if isinstance(o, dict) and o is not opt]}
    out["clarify"] = None
    # the card reads a 'clarify: …' trace line when Answer.clarify is empty (customer.ts clarifyOf): it is answered now
    out["trace"] = [("clarify resolved: " + str(t)[len("clarify: "):]) if str(t).startswith("clarify: ") else t
                    for t in (out.get("trace") or [])]
    learned.append({"id": it["id"], "kind": "choice", "changed": True,
                    "text": f"{cl.get('ask') or 'which one'} → {it['label']} (you picked it earlier)"
                            + (f"; was {ours}" if ours not in (None, out.get("value")) else "")})
    return out


# -- 'The gym is here': a room the visitor pointed at ------------------------------------------------------------------
_PHRASE_AT = re.compile(r"(?i)\b(?:of|in|does|is|for|inside|has|around|about)\s+the\s+([a-z][\w'’-]*(?:\s+[a-z][\w'’-]*){0,3}?)"
                        r"(?=\s+(?:have|has|on|in|at|of|with|and|measure|cover|take|need|on)\b|\s*[?.,!;:]|\s*$)")
_NOT_A_ROOM = re.compile(r"(?i)^(?:(?:floor|gross|net|total|whole|entire)\s+)?(?:area|size|skirting(?:\s+boards?)?|baseboards?|"
                         r"perimeter|length|width|height|number|count|scale|doors?|windows?|walls?|rooms?|spaces?|plan|"
                         r"drawing|sheet|page|building|model|project|floor|level|storey|ground floor|first floor|"
                         r"second floor|basement|roof|site|answer|value)$|\b(?:plan|drawing|sheet|building|floor|level|storey|"
                         r"scale|legend|title block)$")
# the rewrite would drop the other rooms of a question about several ('the gym and the kitchen', 'total', 'each')
_SEVERAL = re.compile(r"(?i)\b(and|or|plus|total|together|combined|each|every|all|both|between|versus|vs|than|compare\w*)\b|[+&]")


def _clean_phrase(ph: str) -> str:
    ph = re.sub(r"(?i)^(?:the|this|that|our|my)\s+", "", re.sub(r"\s+", " ", str(ph or "")).strip(" ?.,!'\"")).strip()
    if len(ph) < 2 or len(ph) > 40 or _NOT_A_ROOM.search(ph) or _OBJECT_WORDS.fullmatch(ph):
        return ""
    return ph


def _room_phrase(question: str) -> str | None:
    """The room a question names in the visitor's words: 'What is the area of the gym?' -> 'gym', 'How many doors does
    the nurse's room have?' -> "nurse's room" (the last 'the X' that is not the measured thing / the sheet)."""
    best = None
    for m in _PHRASE_AT.finditer(str(question or "")):
        ph = _clean_phrase(m.group(1))
        if ph:
            best = ph
    return best


def _room_hint(fid: str, page: int, point, space: str, budget: float = 6.0) -> dict:
    """The room's name as PRINTED where the visitor pointed: the name-like label inside the outline traced at the point
    (PDF) or nearest to it (pictures: the text layer the readers already made), joined with the label lines right
    above / below it ('Multi-Purpose' + 'Hall'); `unique` = that label is printed once on the sheet (several 'Store's:
    the point decides, not the name). {} when nothing / on error / over budget (never blocks the correction)."""
    import threading as _th

    box: dict = {}

    def run() -> None:
        try:
            box.update(_room_hint_now(fid, page, point, space))
        except Exception:  # noqa: BLE001
            pass

    t = _th.Thread(target=run, daemon=True)
    t.start()
    t.join(budget)
    return dict(box) if not t.is_alive() else {}


def _room_hint_now(fid: str, page: int, point, space: str) -> dict:
    from stoa.agent import click as K

    ab, pt = _abs(fid), (float(point[0]), float(point[1]))
    pdf = ab.lower().endswith(".pdf")
    out: dict = {}
    poly = None
    if pdf and space == "pdf_pt":
        r = K.room_at(None, ab, K.Ask(point=pt, space=space, page=page)) or {}
        o = r.get("outline")
        if o is not None and o.evidence:
            out["outline_bbox"] = [round(float(v), 2) for v in o.evidence[0].bbox]
            poly = next((e.polygon for e in o.evidence if e.polygon), None)
    elif pdf:
        return out
    lines = [(str(t).strip(), tuple(map(float, bb))) for t, bb in K.lines(ab, page or 0)]
    cand = [(t, bb) for t, bb in lines if K._NAMEISH.search(t) and not K._NOT_NAME.search(t) and len(t) <= 40
            and sum(c.isdigit() for c in t) <= len(t) / 3 and not _NOT_A_ROOM.search(t)]
    if not cand:
        return out
    seed = None
    if poly:
        ins = [c for c in cand if K.inside(K.centre(c[1]), poly)]
        if ins:
            seed = min(ins, key=lambda c: K._dist_box(pt, c[1]))
    if seed is None:
        best = min(cand, key=lambda c: K._dist_box(pt, c[1]))
        if K._dist_box(pt, best[1]) < 0.05 * K._diag(ab):
            seed = best
    if seed is None:
        return out
    group, used = [seed], {seed}

    def nxt(below: bool):
        ref = group[-1] if below else group[0]
        x0, y0, x1, y1 = ref[1]
        h = max(1e-6, y1 - y0)
        for c in cand:
            if c in used:
                continue
            cx = (c[1][0] + c[1][2]) / 2
            gap = (c[1][1] - y1) if below else (y0 - c[1][3])
            if -0.25 * h <= gap <= 0.8 * h and x0 - 0.1 * h <= cx <= x1 + 0.1 * h and (c[1][3] - c[1][1]) < 1.6 * h:
                return c
        return None

    for below in (True, False):
        for _ in range(2):
            c = nxt(below)
            if c is None:
                break
            used.add(c)
            group = [*group, c] if below else [c, *group]
    name = ""
    for t, _ in group:
        name = (name[:-1] + t) if name.endswith("-") else (f"{name} {t}" if name else t)
    name = re.sub(r"\s+", " ", name).strip()[:60]
    if len(name) >= 3:
        out["label"] = name
        out["label_lines"] = [t for t, _ in group]  # as printed, top to bottom (another sheet of the set: _prints_once)
        out["unique"] = sum(1 for t, _ in cand if t.lower() == seed[0].lower()) == 1
    return out


_PRINTS: dict[tuple[str, int, str], bool] = {}


def _prints_once(fid: str, lines_of: list[str], page: int = 0, budget: float = 2.0) -> bool:
    """Another sheet of the set prints this room label once ('Multi-Purpose' + 'Hall': the first line exactly once, the
    others somewhere): the visitor's word for it carries there (the scan copy of the same plan, a revision). Cached;
    False over budget (a picture whose text layer is not made yet) — never slows a question by more than `budget`."""
    import threading as _th

    key = (fid, int(page or 0), "\n".join(lines_of).lower())
    if key in _PRINTS:
        return _PRINTS[key]
    box: dict = {}

    def run() -> None:
        try:
            from stoa.agent import click as K

            texts = [re.sub(r"\s+", " ", str(t)).strip().lower() for t, _ in K.lines(_abs(fid), int(page or 0))]
            first, rest = lines_of[0].strip().lower(), [x.strip().lower() for x in lines_of[1:]]
            box["ok"] = texts.count(first) == 1 and all(x in texts for x in rest)
        except Exception:  # noqa: BLE001
            box["ok"] = False

    t = _th.Thread(target=run, daemon=True)
    t.start()
    t.join(budget)
    if t.is_alive():
        return False
    _PRINTS[key] = bool(box.get("ok"))
    return _PRINTS[key]


def _apply_rooms(fid: str, q: str, items: list[dict], ctx: dict, page: int | None) -> str:
    """'The gym is here' (newest first). The name printed where they pointed, when it is printed once on the sheet,
    replaces their word ('the gym' -> 'the Multi-Purpose Hall': every reader understands it, pictures too); otherwise
    (several rooms of that name, nothing printed there) the question is asked about 'this room' + the point, so the
    pipeline's click context (stoa.agent.click: the room traced at the point) answers it. A question about several
    rooms only gets the alias (the point would answer for this one room only)."""
    for c in reversed([i for i in items if i["kind"] == "room" and i["file"] == fid and i.get("point") and i.get("phrase")]):
        if page is not None and int(c.get("page") or 0) != page:
            continue
        pat = re.compile(rf"(?i)\b(?:the|this|that|our|my)\s+{re.escape(str(c['phrase']))}(?:'s)?(?![\w'’-])")
        m = pat.search(q)
        if not m:
            continue
        printed = str(c.get("label") or "").strip() if c.get("unique") else ""
        alias = printed or str(c.get("name") or "").strip()
        rest = q[:m.start()] + " " + q[m.end():]
        several = bool(_SEVERAL.search(rest) or _room_phrase(rest.replace("  ", " ")))
        if alias and alias.lower() != str(c["phrase"]).lower() and (printed or several):
            if re.search(rf"(?i)\b{re.escape(alias)}\b", q):
                continue
            how = "printed where you pointed" if printed else "the room you pointed at"
            ctx["used"].append({"id": c["id"], "kind": "room", "changed": True, "text": f"'{c['phrase']}' = {alias} ({how})"})
            q = pat.sub(f"the {alias}", q, count=1)
            if several:
                continue
            return q
        if several:
            ctx.setdefault("skipped", []).append({"id": c["id"], "why": "the question names more than one room"})
            continue
        if c.get("label") and not c.get("unique") and _task_of(q) != "area":
            # one of several rooms printed with that name: the readers pick the one at the point for AREAS (each traced
            # outline is matched to the point); a skirting / door question about 'this room' falls back to a one-line
            # name ('the Secondary' -> a text search '20' @0.3 on DfE L00), worse than the visitor's own question
            ctx.setdefault("skipped", []).append({"id": c["id"], "why": f"several rooms are named {c['label']}: only areas "
                                                  "are read at the point"})
            continue
        ctx["context"] = {"point": list(c["point"]), "space": c.get("space") or "pdf_pt", "page": int(c.get("page") or 0)}
        ctx["used"].append({"id": c["id"], "kind": "room", "changed": True, "phrase": c["phrase"], "hint": c.get("label"),
                            "name": c.get("name"), "point": list(c["point"]), "space": c.get("space"), "at": True,
                            "text": f"'{c['phrase']}' = the room you pointed at"})
        # a thing in that room: 'the gym door' -> 'the door of this room' ('this room door' read as the room's own width)
        t = re.match(rf"(?i)\s+((?:(?:north|south|east|west|front|back|rear|side|main|entrance|inner|outer|left|right)\s+)?"
                     rf"{_THING})(?![\w'’-])", q[m.end():])
        if t:
            return q[:m.start()] + f"the {t.group(1)} of this room" + q[m.end() + t.end():]
        return pat.sub("this room", q, count=1)
    # another sheet of the set (the scan copy of the same plan, a revision): the word carries when this sheet prints the
    # same label once (no point: the coordinates are another drawing's)
    done = {str(i.get("phrase") or "").lower() for i in items if i["kind"] == "room" and i["file"] == fid}
    for c in reversed([i for i in items if i["kind"] == "room" and i["file"] != fid and i.get("unique") and i.get("label")
                       and i.get("label_lines") and i.get("phrase")]):
        if str(c["phrase"]).lower() in done:
            continue
        pat = re.compile(rf"(?i)\b(?:the|this|that|our|my)\s+{re.escape(str(c['phrase']))}(?:'s)?(?![\w'’-])")
        lab = str(c["label"]).strip()
        if not pat.search(q) or lab.lower() == str(c["phrase"]).lower() or re.search(rf"(?i)\b{re.escape(lab)}\b", q):
            continue
        if not _prints_once(fid, list(c["label_lines"]), page or 0):
            continue
        done.add(str(c["phrase"]).lower())
        ctx["used"].append({"id": c["id"], "kind": "room", "changed": True,
                            "text": f"'{c['phrase']}' = {lab} (you showed it on {_sheet_name(c['file'])})"})
        q = pat.sub(f"the {lab}", q, count=1)
    return q


def _room_named(out: dict, u: dict, project: str | None = None) -> dict:
    """The learned line names the room the answer measured ('Multi-Purpose Hall: 246.46 m² traced …' -> that name) when
    that evidence is AT the point or starts with what is printed there; the first such name is kept on the item (so a
    question about several rooms can use it as an alias)."""
    if not u.get("at"):
        return u
    pt, hint = u.get("point") or [], str(u.get("hint") or "")
    for e in out.get("evidence") or []:
        e = e or {}
        m = re.match(r"\s*([A-Z][\w'’ /&().-]{2,48}?):\s", str(e.get("note") or ""))
        if not m or re.match(r"(?i)(double|single|door|window|graphic|measured|our reading|skirting|scale)\b", m.group(1)):
            continue
        name, bb = m.group(1).strip(), e.get("bbox") or []
        at = (len(bb) == 4 and len(pt) == 2 and e.get("bbox_space") == u.get("space")
              and bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3])
        if at or (len(hint) >= 4 and name.lower().startswith(hint.lower()[:12])):
            if project and name != u.get("name"):
                _keep_room_name(project, u["id"], name)
            return {**u, "text": f"{u['text']} ({name})"}
    if u.get("name"):
        return {**u, "text": f"{u['text']} ({u['name']})"}
    return {**u, "text": u["text"] + (f" (printed there: {hint})" if hint else "")}


def _keep_room_name(project: str, item_id: str, name: str) -> None:
    try:
        data = load(project)
        data = {**data, "items": [dict(i) for i in data["items"]]}
        for it in data["items"]:
            if it["id"] == item_id and it.get("kind") == "room":
                it["name"] = name[:60]
        save(project, data)  # new revision: a cached 'gym and kitchen' answer from before the name must not be served
    except Exception:  # noqa: BLE001
        pass


def _apply_sheet(fid, out, items, learned):
    sh = [i for i in items if i["kind"] == "sheet" and i["file"] == fid and not i.get("read_as")]  # read-as: wrap_upload
    if not sh:
        return
    it = sh[-1]
    rec = dict(out.get("recognition") or {})
    said = []
    for k in ("kind", "label", "storey", "title"):
        if it.get(k):
            rec[k] = it[k]
            said.append(f"{k} = {it[k]}")
    if said:
        rec["by_user"] = True
        out["recognition"] = rec
        learned.append({"id": it["id"], "kind": "sheet", "changed": False, "text": "sheet as you named it: " + ", ".join(said)})


# -- convenience: one question with the hooks around any solver ---------------------------------------------------------
def ask_with(solve, path: str, question: str, solver_args: dict | None = None, project: str | None = None) -> dict:
    """before -> solve(path, question, solver_args) -> after; `solve` returns an answer dict."""
    q2, args, ctx = before(path, question, solver_args, project)
    p2 = ctx.get("abs") or path  # a sheet that prints no scale: the copy that prints the user's (see _stamped)
    out = solve(p2, q2, args)
    return after(path, question, out, ctx, resolve=lambda q: solve(p2, q, args))


def _known_solver(name: str) -> bool:
    """A remembered 'read it as' solver that this tree does not have must not break every later question."""
    try:
        from stoa.solvers import available

        return name in available()
    except Exception:  # noqa: BLE001
        return False


def wrap_upload(answer_fn, req: dict) -> dict:
    """web/server/ask_upload.answer(req) with the hooks around it (W5 wires: `return wrap_upload(_answer, req)`).
    req = {abs, virtual, question, solver_args, page?, project?}. Off (no learn arg, feature not listed): passes through."""
    try:
        sa = dict(req.get("solver_args") or {})
        # ONLY the request's own opt-in (the web plugin sends learn + visitor when ?features=corrections is on): with the
        # web flag off nothing is applied or mentioned, even when the API mounts the feature (QA 21:43 off-check)
        on = "learn" in sa and enabled(sa)
        # the answer on the visitor's screen (sent once, with their FIRST correction of a set when it is a symbol one:
        # that answer was asked without learn, so no reading of it was kept here); never passed to a reader
        shown = sa.pop("shown", None)
        if not on:
            for k in ("learn", "learn_project", "visitor", "read_as_off"):
                sa.pop(k, None)
        else:
            where = req.get("virtual") or req.get("abs") or ""
            asked = str(req.get("question", ""))
            vis = str(sa.get("visitor") or "")
            pg = req.get("page")
            rc = req.get("context") if isinstance(req.get("context"), dict) else {}
            own = {k: v for k, v in rc.items() if k in ("point", "region", "view") and v}  # askWith always sends page
            q, args, ctx = before(where, asked, sa, req.get("project"), page=pg if isinstance(pg, int) else rc.get("page"),
                                  context=own or None)
            # the virtual path stays the user's file: ask_upload names evidence on the solved file by it
            r2 = {**req, "abs": ctx.get("abs") or req.get("abs"), "solver_args": args}
            if ctx.get("context"):  # 'The gym is here': the room at the visitor's point (stoa.agent.click)
                r2["context"] = {**rc, **ctx["context"]}
            ra = ctx.get("read_as")
            # no reader picked for THIS question: web/src/api.ts sends solver 'pipeline' whenever a plug-in adds ask
            # args (ours do), so 'pipeline' means 'automatic' here too (a reload asked with it: applied 0 times before)
            if ra and str(req.get("solver") or "pipeline") == "pipeline" and _known_solver(ra["solver"]):
                r2["solver"] = ra["solver"]
                ctx["used"].append({"id": ra["id"], "kind": "sheet", "changed": True,
                                    "text": f"read as {ra['label']} (you chose it for this drawing)"})
            elif str(req.get("solver") or "") == "pipeline":
                # api.ts turns 'no solver' into 'pipeline' as soon as a plug-in adds ask args (ours, on every upload
                # question): ask_upload then skipped its default 'upload' solver (P1's normaliser: traced copy of a
                # picture / scan, plan page of a set …) -> worse answers on pictures with corrections ON (DfE PNG
                # 'area of the Multi-Purpose Hall' 287.87 @agent vs 247.73 plan_rooms). Automatic = ask_upload's default
                r2["solver"] = ""
    except Exception:  # noqa: BLE001 — fail closed: the request as it came, without our switch
        sa = {k: v for k, v in dict(req.get("solver_args") or {}).items()
              if k not in ("learn", "learn_project", "visitor", "read_as_off", "shown")}
        on = False
    if not on:
        return answer_fn({**req, "solver_args": sa})
    kept = kept_answer(ctx.get("file") or where, asked, ctx)
    if kept is not None:
        # the scale / room / read-as lines did not shape this answer: only the kept value is reported
        return after(where, asked, kept, {**ctx, "used": []}, resolve=None)
    # UX C-0 (01:15): after 'This is not a door' the re-ask read the sheet AGAIN — on the preview another reader (Gemini 17
    # vs local 16), so 'minus one' made the count go UP. A symbol correction (not a door / a door we missed / count all
    # like this) changes nothing a reader sees: the reading on screen is corrected, not read again. Any other change
    # (scale, room, read-as, a pick, a value) or another question / page / context / reader -> read as usual.
    key = _raw_key(vis, ctx.get("file") or where, r2, q)
    sig = _raw_sig(ctx.get("items") or [])
    old = _raw_get(ctx.get("project"), key)
    if old is not None and time.time() - old[0] < RAW_TTL and old[1] == sig:
        out = json.loads(old[2])
        out["cost"] = {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0, "wall_seconds": 0.0}
        out["trace"] = [*(out.get("trace") or []), "learn: the reading on screen, corrected (the drawing was not read again)"]
    elif _seed_ok(shown, sig):
        # UX 02:55: a visitor without corrections asks plainly (the pre-warmed answers); their first 'This is not a door'
        # then corrects the answer they SAW (the client sends it once), not a fresh reading by maybe another reader
        raw = {k: v for k, v in shown.items() if k not in ("learned", "cached")}
        _raw_put(ctx.get("project"), key, sig, raw)
        out = json.loads(json.dumps(raw, default=str))
        out["cost"] = {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0.0, "api_calls": 0, "wall_seconds": 0.0}
        out["trace"] = [*(out.get("trace") or []), "learn: the reading on screen, corrected (the drawing was not read again)"]
    else:
        out = answer_fn({**r2, "question": q})  # the solver's own errors reach the caller as they would without us
        if isinstance(out, dict) and out.get("value") is not None:
            _raw_put(ctx.get("project"), key, sig, out)
    return after(where, asked, out, ctx, resolve=lambda q2: answer_fn({**r2, "question": q2}))


# the last raw reading per (visitor, file, page, question, reader, context): a symbol correction corrects THIS reading.
# In memory AND on disk next to the project's memory (.stoa_raw/): Vite runs TWO upload workers, and the re-ask after a
# correction often lands on the other one (walk c7-c0: the first re-ask read the picture again)
_RAW: dict[tuple, tuple[float, str, str]] = {}
RAW_TTL = 900.0


def _raw_file(project: str | None, key: tuple) -> Path | None:
    if not project:
        return None
    return store_path(project).parent / ".stoa_raw" / (hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:20] + ".json")


def _raw_get(project: str | None, key: tuple) -> tuple[float, str, str] | None:
    try:
        f = _raw_file(project, key)
        if f is not None and f.is_file():
            d = json.loads(f.read_text())
            return (float(d["t"]), str(d["sig"]), str(d["out"]))
    except Exception:  # noqa: BLE001 — a cache: a miss
        pass
    return _RAW.get(key)


def _raw_put(project: str | None, key: tuple, sig: str, out: dict) -> None:
    try:
        rec = (time.time(), sig, json.dumps(out, default=str))
        _RAW[key] = rec
        while len(_RAW) > 200:
            _RAW.pop(next(iter(_RAW)))
        f = _raw_file(project, key)
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            for old in f.parent.glob("*.json"):  # readings live RAW_TTL at most (privacy: nothing kept for long)
                try:
                    if time.time() - old.stat().st_mtime > RAW_TTL:
                        old.unlink()
                except OSError:
                    pass
            fd, tmp = tempfile.mkstemp(dir=f.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump({"t": rec[0], "sig": sig, "out": rec[2]}, fh)
            os.replace(tmp, f)
    except Exception:  # noqa: BLE001 — a cache, never the answer's problem
        pass


def _raw_key(visitor: str, fid: str, r2: dict, q: str) -> tuple:
    # only what changes the reading: the question box and the plug-ins' askWith send page / context differently
    # ({page} alone is no context), so a point / region / view counts, the page wherever it was sent
    c = r2.get("context") if isinstance(r2.get("context"), dict) else {}
    page = r2.get("page") if r2.get("page") is not None else c.get("page")
    own = {k: c[k] for k in ("point", "region", "view") if c.get(k)}
    # every other switch the request carries (another tool's scale arg, private mode, Gemini on/off) reads differently
    sa = {k: v for k, v in dict(r2.get("solver_args") or {}).items() if k not in ("learn", "visitor", "read_as_off", "learn_project")}
    return (visitor, fid, str(r2.get("abs") or ""), int(page or 0), _norm_q(q), str(r2.get("solver") or ""),
            json.dumps(own, sort_keys=True, default=str), json.dumps(sa, sort_keys=True, default=str),
            bool(r2.get("private")), bool(r2.get("eu")))


def _seed_ok(shown, sig: str) -> bool:
    """The answer the client says is on screen may stand in for the reading (wrap_upload) only when it is a plain reading
    (no correction applied to it, nothing kept) and no correction that changes what a reader sees is active (sig '')."""
    if not isinstance(shown, dict) or sig or shown.get("value") is None or shown.get("learned"):
        return False
    if not isinstance(shown.get("evidence", []), list) or not isinstance(shown.get("trace", []), list):
        return False
    if any(str(t).startswith(("learn:", "kept:")) for t in shown.get("trace") or []):
        return False
    try:
        return len(json.dumps(shown, default=str)) < 4_000_000
    except Exception:  # noqa: BLE001
        return False


def _raw_sig(items: list[dict]) -> str:
    """Every active correction that changes what a reader sees or is asked (all but symbols)."""
    return ",".join(sorted(str(i.get("id")) for i in items if i.get("active", True) and i.get("kind") != "symbol"))


# -- HTTP (prefix /feat/corrections; S0 mounts the features router in stoa.api) ------------------------------------------
try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field

    router = APIRouter(prefix="/feat/corrections", tags=["F7 corrections"])

    class CorrectionIn(BaseModel):
        path: str
        kind: str
        project: str | None = None
        page: int | None = None
        # scale
        ratio: str | int | float | None = None
        px_per_m: float | None = None
        units_per_m: float | None = None
        space: str | None = None
        scope: str | None = None
        # choice
        ask: str | None = None
        label: str | None = None
        phrase: str | None = None
        question: str | None = None
        why: str | None = None  # the clarification's why ('two readings disagree'): a reading pick vs a scope pick
        asked: str | None = None  # the question the user had asked when they picked
        # symbol
        bbox: list[float] | None = None
        bbox_space: str | None = None
        bboxes: list[list[float]] | None = None  # symbol: the mark + the look-alikes the user accepted ('N more like it')
        # 'is' is a keyword in Python: the symbol's yes / no ("this is a door" / "this is not a door")
        is_: bool | None = Field(None, alias="is")
        # value
        value: float | int | str | None = None
        unit: str | None = None
        note: str | None = None
        # sheet
        title: str | None = None
        storey: str | None = None
        read_as: str | None = None  # sheet: the solver the visitor chose with 'Change' -> 'Read it as …'
        # value: what the number is of, how it was got ('measure' = the measure tool), the user's outline / line
        subject: str | None = None
        task: str | None = None
        source: str | None = None
        traced: bool | None = None  # value: OUR traced outline the visitor accepted (Measure 'Suggest' -> 'Use it')
        evidence: list[dict] | None = None
        visitor: str | None = None  # the browser's random id: corrections apply to that visitor's answers only
        # room ('The gym is here'): phrase + point [x, y] in space, page
        point: list[float] | None = None

        model_config = {"populate_by_name": True}

    def _view(path: str, project: str | None, visitor: str | None = None) -> dict:
        data = load(project_of(path, project))
        mine = items_for(path, project, active_only=False, visitor=visitor)
        return {"project": data.get("project") or project_of(path, project), "rev": data.get("rev", 0),
                "items": list(reversed(mine)), "file": file_id(path), "kinds": list(KINDS), "on": enabled()}

    @router.get("")
    def list_corrections(path: str, project: str | None = None, visitor: str | None = None) -> dict:
        return _view(path, project, visitor)

    @router.post("")
    def add_correction(c: CorrectionIn) -> dict:
        payload = {k: v for k, v in c.model_dump().items() if v is not None and k not in ("path", "kind", "project", "is_")}
        if c.kind == "symbol":
            payload["is"] = True if c.is_ is None else bool(c.is_)
        try:
            item = add(c.path, c.kind, payload, c.project)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"item": item, **_view(c.path, c.project, c.visitor)}

    class ForgetIn(BaseModel):
        visitor: str
        keep: list[str] | None = None  # paths whose project stays (own uploads the visitor did not agree to forget)
        path: str | None = None  # the open file: its view comes back

    @router.post("/forget")
    def forget_all(c: ForgetIn) -> dict:
        try:
            res = forget(c.visitor, c.keep)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**res, **(_view(c.path, None, c.visitor) if c.path else {})}

    @router.post("/{item_id}/undo")
    def undo(item_id: str, path: str, project: str | None = None, visitor: str | None = None) -> dict:
        if set_active(path, item_id, False, project) is None:
            raise HTTPException(404, "no such correction")
        return _view(path, project, visitor)

    @router.post("/{item_id}/redo")
    def redo(item_id: str, path: str, project: str | None = None, visitor: str | None = None) -> dict:
        if set_active(path, item_id, True, project) is None:
            raise HTTPException(404, "no such correction")
        return _view(path, project, visitor)

    @router.delete("/{item_id}")
    def delete(item_id: str, path: str, project: str | None = None, visitor: str | None = None) -> dict:
        if not remove(path, item_id, project):
            raise HTTPException(404, "no such correction")
        return _view(path, project, visitor)

    class SimilarIn(BaseModel):
        path: str
        page: int | None = 0
        bbox: list[float]
        bbox_space: str | None = None
        exclude: list[list[float]] | None = None  # the answer's own boxes (already counted; they also give the size)
        label: str | None = None

    @router.post("/alike")
    def find_alike(req: SimilarIn) -> dict:
        """'This is not a door': the other boxes this answer counted (``exclude`` = the answer's boxes on this page)
        that look like the rejected one at ``bbox``. Never raises: no hits + reason."""
        try:
            r = alike(req.path, req.page, req.bbox, req.bbox_space, req.exclude)
        except Exception as e:  # noqa: BLE001
            r = {"boxes": [], "scores": [], "mark": req.bbox, "counted": 0, "reason": f"could not match: {type(e).__name__}"}
        return {**r, "label": req.label, "page": req.page, "bbox_space": req.bbox_space}

    @router.post("/similar")
    def find_similar(req: SimilarIn) -> dict:
        """'N more like it': the symbols on the page that look like the one the user just marked, minus the ones the
        answer already counted. Template matching on the page render, ~1-2 s, $0. Never raises: no hits + reason."""
        try:
            r = similar(req.path, req.page, req.bbox, req.bbox_space, req.exclude)
        except Exception as e:  # noqa: BLE001 — a missing file / an odd picture: offer nothing
            r = {"boxes": [], "scores": [], "mark": req.bbox, "counted": 0, "reason": f"could not match: {type(e).__name__}"}
        return {**r, "label": req.label, "page": req.page, "bbox_space": req.bbox_space}

    class AskIn(BaseModel):
        path: str
        question: str
        solver: str = "pipeline"
        solver_args: dict = {}
        project: str | None = None

    @router.post("/ask")
    def ask(req: AskIn) -> dict:
        """POST /ask with the user's corrections applied (dataset paths; uploads go through web/server/ask_upload.py,
        which calls before()/after() itself when the hook is wired)."""
        from stoa import api

        # T2 c8 (experiments/t2/route_probe2.py): solve() below answers an ABSOLUTE path directly (meant for the stamped
        # copy before() makes), and nothing checked that the REQUEST's path was not one: POST /api/feat/corrections/ask
        # {"path": "/any/file.pdf"} read any drawing on the server through the web proxy ('SECRET ROOM 101 Kitchen' from a
        # canary PDF in /tmp). The request's path must be dataset-relative or an upload ref; only before() makes an
        # absolute one. STOA_CORRECTIONS_ASK_ANY_PATH=1 restores the old behaviour.
        p0 = str(req.path or "")
        if os.environ.get("STOA_CORRECTIONS_ASK_ANY_PATH", "0") != "1" and (
                p0.startswith(("/", "\\", "~")) or Path(p0).is_absolute() or "://" in p0 or "\\" in p0
                or ".." in Path(p0).parts or re.match(r"^[A-Za-z]:", p0)):
            raise HTTPException(400, "path must be dataset-relative (data/...) or an upload (upload/<id>/<name>), "
                                     "without '..'")

        def solve(p, q, args):
            if Path(p).is_absolute():  # the copy that prints the user's scale (outside the dataset: no api.ask)
                from stoa.contract import Answer

                ans = api._solver(req.solver, {**args, "learn": 0}).solve(p, q)
                return (ans if isinstance(ans, Answer) else Answer.model_validate(ans)).model_dump()
            r = api.AskRequest(path=p, question=q, solver=req.solver, solver_args={**args, "learn": 0})
            return dict(api.ask(r))

        return ask_with(solve, req.path, req.question, {**req.solver_args, "learn": 1}, req.project)
except ImportError:  # pragma: no cover — fastapi missing: the hooks still work
    router = None
