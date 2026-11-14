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

KINDS = ("scale", "choice", "symbol", "value", "sheet")
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
        if item.get("bboxes"):  # 'N more like it': the mark first, then the look-alikes the user accepted
            bbs = [list(map(float, b)) for b in item["bboxes"] if isinstance(b, (list, tuple)) and len(b) == 4]
            if not item["is"] or not bbs:
                raise ValueError("bboxes: a list of [x0, y0, x1, y1] for 'this is a …' only")
            item["bboxes"] = bbs[:SIMILAR_CAP + 1]
    elif kind == "value":
        if item.get("value") in (None, "") or not item.get("question"):
            raise ValueError("a value correction needs question and value")
        item["question_norm"] = _norm_q(item["question"])
        # what the number is OF, so a reworded question finds it too ('How big is the Kitchen?' after 'What is the
        # floor area of the Kitchen?'): the client's subject (the room the user measured), else read from the question
        item["task"] = item.get("task") or _task_of(item["question"])
        item["subject"] = str(item.get("subject") or _subject_of(item["question"]) or "").strip() or None
    data = load(proj)
    data = {**data, "items": [dict(i) for i in data["items"]]}
    for old in data["items"]:
        if not old.get("active") or old["kind"] != kind or old["file"] != fid:
            continue
        same = (kind == "scale" or (kind == "sheet" and bool(old.get("read_as")) == bool(item.get("read_as")))
                or (kind == "value" and old.get("question_norm") == item.get("question_norm"))
                or (kind == "choice" and old.get("ask_norm") == item.get("ask_norm") and old.get("phrase") == item.get("phrase")
                    and (item.get("pick") != "reading" or (old.get("asked_norm"), old.get("subject"))
                         == (item.get("asked_norm"), item.get("subject"))))
                or (kind == "symbol" and old.get("label") == item["label"] and (_iou(old.get("bbox"), item["bbox"]) > 0.5 or (
                    item.get("bboxes") and old.get("is") and _overlaps(old.get("bbox"), item["bbox"])))))
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


def items_for(path: str, project: str | None = None, active_only: bool = True) -> list[dict]:
    data = load(project_of(path, project))
    return [i for i in data["items"] if i.get("active") or not active_only]


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


def before(path: str, question: str, solver_args: dict | None = None, project: str | None = None
           ) -> tuple[str, dict, dict]:
    """Apply what the user taught us BEFORE the solver runs: their scale (solver_args scale / px_per_m, unless the
    request sets one itself) and room aliases from earlier picks. Returns (question, solver_args, ctx for after())."""
    args = dict(solver_args or {})
    ctx = {"used": [], "on": False, "project": None, "file": None, "asked": question}
    if not enabled(args):
        args.pop("learn", None)
        return question, args, ctx
    args.pop("learn", None)
    project = project or args.pop("learn_project", None)
    try:
        fid, proj = file_id(path), project_of(path, project)
        items = items_for(path, project)
        ctx.update(on=True, project=proj, file=fid, items=items)
        if not items:
            return question, args, ctx
        ra = [i for i in items if i["kind"] == "sheet" and i["file"] == fid and i.get("read_as")]
        if ra:  # the visitor's 'Change' -> 'Read it as …' on the recognition chip (applied by wrap_upload: the solver)
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
            else:
                learned.append(u)
        out = _apply_values(fid, question, out, items, learned)
        out = _apply_symbols(fid, question, out, items, learned)
        out = _apply_choice(fid, question, out, items, learned, resolve)
        _apply_sheet(fid, out, items, learned)
        if learned:
            out["learned"] = [{k: v for k, v in x.items() if k in ("id", "kind", "text", "changed", "confirmed")} for x in learned]
            lead = [x for x in learned if x.get("changed")]
            if lead:
                out["assumptions"] = ["Improved using your correction: " + "; ".join(x["text"] for x in lead),
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
    """The named thing a question is about: 'the Multi-Purpose Hall', 'the Kitchen', 'Room 204' (capitalised words)."""
    for m in _SUBJ.finditer(str(question or "")):
        sub = re.sub(r"(?i)\s+(room|space|area)$", "", m.group(1)).strip()
        if sub and sub.lower() not in ("the", "this", "it", "there", "what", "how"):
            return sub
    return None


def _task_of(question: str) -> str | None:
    q = str(question or "")
    if re.search(r"(?i)\b(skirting|base ?boards?|perimeter|length|long|wide|width|linear|running)\b", q):
        return "length"
    if re.search(r"(?i)\bhow many\s+(square|sq\.?\s|m2|m²|ft2|ft²)", q):  # 'how many square metres is it?'
        return "area"
    if re.search(r"(?i)\b(how many|number of|count)\b", q):
        return "count"
    if re.search(r"(?i)\b(area|how big|size|square|m2|m²|sq ?m|sq ?ft|ft2)\b", q):
        return "area"
    return None


def _apply_values(fid, question, out, items, learned):
    qn = _norm_q(question)
    task = _task_of(question)
    hit = [i for i in items if i["kind"] == "value" and i["file"] == fid and (
        i.get("question_norm") == qn
        or (i.get("subject") and i.get("task") and i["task"] == task
            and re.search(rf"(?i)\b{re.escape(i['subject'])}\b", question)
            and not re.search(r"(?i)\b(total|sum|all|each|and)\b", question)))]
    if not hit:
        return out
    it = hit[-1]
    ours = out.get("value")
    out["value"], out["unit"] = it["value"], it.get("unit") or out.get("unit")
    out["confidence"] = max(float(out.get("confidence") or 0), 0.95)
    out.setdefault("evidence", [])
    # a hand measurement (W5's measure tool: the outline / line the user drew) is the evidence of their number; ours
    # stays listed after it, marked as our reading, so both can be compared on the drawing
    mine = [dict(e) for e in it.get("evidence") or [] if isinstance(e, dict)]
    shown_unit = {"m2": "m²", "ft2": "ft²", "m3": "m³"}.get(str(it.get("unit") or ""), it.get("unit") or "")
    for e in mine:
        e.setdefault("file", fid)
        # '<Room>: <n> m² …' is how the card names the room it shows ('floor area of the kitchen', customer.ts noteRoom)
        e["note"] = e.get("note") or (f"{it['subject']}: {it['value']}{(' ' + shown_unit) if shown_unit else ''} measured by you"
                                      if it.get("subject") else
                                      f"measured by you: {it['value']}{(' ' + shown_unit) if shown_unit else ''}")
        e.setdefault("exact", False)
    agrees = _close(ours, it["value"])
    if mine and not agrees:
        # ours stays listed after it as 'our reading'; its '<n> m²' is written 'sq m' so the card does not count two
        # outlines as two rooms ('floor area of 2 kitchens together', customer.ts heroSentence counts '<n> m²' notes)
        old_ev = [{**e, "note": re.sub(r"(\d)\s*(?:m²|m2)(?!\w)", r"\1 sq m", f"our reading ({ours}): {e.get('note') or ''}".rstrip(": "))}
                  for e in out["evidence"]]
        out["evidence"] = [*mine, *old_ev]
    elif mine and not out["evidence"]:
        out["evidence"] = mine
    how = "you measured" if it.get("source") == "measure" or mine else "you gave"
    if agrees:  # their measurement says the same as ours: a confirmation, not a change
        learned.append({"id": it["id"], "kind": "value", "changed": False, "confirmed": True,
                        "text": f"{how} the same {it['value']}{(' ' + shown_unit) if shown_unit else ''}"
                                + (f" for the {it['subject']}" if it.get("subject") else "")})
        return out
    learned.append({"id": it["id"], "kind": "value", "changed": ours != it["value"],
                    "text": f"{how} {it['value']}{(' ' + shown_unit) if shown_unit else ''}"
                            + (f" for the {it['subject']}" if it.get("subject") else " for this question")
                            + (f" (we read {ours})" if ours not in (None, it["value"]) else "")
                            + (f": {it['note']}" if it.get("note") else "")})
    return out


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
    if not syms or not re.search(r"(?i)\b(how many|number of|count|anzahl|wie viele|kuinka monta|combien)\b", question):
        return out
    target = _target_of(question)
    v = out.get("value")
    if not target or not isinstance(v, (int, float)) or isinstance(v, bool):
        return out
    mods = set(re.findall(r"(?i)\b(double|single|sliding|fire|external|internal|exterior|interior|revolving)\b", question))
    ev = list(out.get("evidence") or [])
    boxes = [e for e in ev if e.get("bbox") and (e.get("file") in (None, "", fid) or Path(str(e.get("file"))).name == Path(fid).name)]
    added = removed = liked = 0
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
        same = [e for e in boxes if (page is None or e.get("page") in (None, page))
                and (not s.get("bbox_space") or not e.get("bbox_space") or e.get("bbox_space") == s.get("bbox_space"))]
        over = [e for e in same if _overlaps(e["bbox"], s["bbox"])]
        if s["is"]:
            # one mark, or a mark + the look-alikes the user accepted ('N more like it'): each counts once, where the
            # answer counted, and never on a box already counted (by the reader or by another of the user's marks)
            group = s.get("bboxes") or [s["bbox"]]
            n0 = added
            for j, bb in enumerate(group):
                if any(_overlaps(e["bbox"], bb) for e in same) or any(_overlaps(b, bb) for b in mine):
                    continue
                if not _inside_scope(bb, same):
                    continue
                box = list(bb) if s.get("bboxes") else _sized(bb, same)
                ev.append({"file": fid, "page": page, "bbox": box, "bbox_space": s.get("bbox_space") or
                           (same[0].get("bbox_space") if same else None),
                           "note": f"{lab} — marked by you" if j == 0 else f"{lab} — like the one you marked", "exact": True})
                mine.append(box)
                added += 1
                liked += j > 0
            if added > n0:
                used.append(s)
        elif not s["is"] and over:
            for e in over:
                ev.remove(e)
                boxes.remove(e)
            removed += len(over)
            used.append(s)
    if not used:
        return out
    out["value"] = v + added - removed
    out["evidence"] = ev
    bits = ([f"+{added} {target}{'s' if added != 1 else ''} you marked" + (f" ({liked} found like yours)" if liked else "")]
            if added else []) + (
        [f"−{removed} you said {'are' if removed != 1 else 'is'} not a {target}"] if removed else [])
    for s in used:
        learned.append({"id": s["id"], "kind": "symbol", "changed": True,
                        "text": f"{', '.join(bits)} (we counted {v})"})
        bits = []  # the sentence once; the other items only count as applied
    learned[:] = [x for x in learned if x.get("text")]
    return out


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


def _snap(inv, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int] | None:
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
    solid = cv2.dilate(cv2.erode((patch > 128).astype(np.uint8), np.ones((4, 4), np.uint8)), np.ones((5, 5), np.uint8))
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
        return None
    bx0, by0, bx1, by1 = best
    return max(0, bx0 - 1), max(0, by0 - 1), min(W, bx1 + 1), min(H, by1 + 1)


def similar(path: str, page: int | None, bbox, bbox_space: str | None = None, exclude=None, thr: float = SIMILAR_THR,
            cap: int = SIMILAR_CAP) -> dict:
    """The symbols on the page that look like the one the user marked at ``bbox`` (template matching, no model): the
    mark at the size of the symbols the reader boxed (``exclude``: the answer's own boxes, already counted), matched in
    its 8 orientations (4 rotations x mirror), TM_CCOEFF_NORMED >= thr, one hit per symbol. Hits on a counted box or on
    the mark itself are left out (``counted``: how many fell on counted boxes: a sanity check that the mark looks like
    what the reader counts). Returns {boxes: [[x0, y0, x1, y1] in bbox_space], mark, counted, scores, secs, reason}."""
    import numpy as np
    import cv2

    t0 = time.time()
    fid = file_id(path)
    ex = [list(map(float, b)) for b in (exclude or []) if b and len(b) == 4]
    mark = _sized(list(map(float, bbox)), [{"bbox": b} for b in ex])
    img, k = _page_gray(fid, page or 0, bbox_space)
    x0, y0 = max(0, int(round(mark[0] * k))), max(0, int(round(mark[1] * k)))
    x1, y1 = min(img.shape[1], int(round(mark[2] * k))), min(img.shape[0], int(round(mark[3] * k)))
    out = {"boxes": [], "scores": [], "mark": mark, "counted": 0, "thr": thr, "secs": 0.0, "reason": None}
    if x1 - x0 < 6 or y1 - y0 < 6:
        out["reason"] = "the mark is too small to match"
        return out
    inv = _ink(img)
    snapped = _snap(inv, x0, y0, x1, y1)
    if snapped is None:  # nothing thin under the click (a wall, a fill, a blurred scan): matching the loose box floods
        out["reason"] = "no single symbol found under the mark (a wall, a fill, or too blurred to cut out)"
        return out
    x0, y0, x1, y1 = snapped
    mark = [round(x0 / k, 2), round(y0 / k, 2), round(x1 / k, 2), round(y1 / k, 2)]
    out["mark"] = mark
    tpl = inv[y0:y1, x0:x1]
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
        return bool(sub and i.get("task") == task and re.search(rf"(?i)\b{re.escape(sub)}\b", question))

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
        on = enabled(sa)
        if not on:
            sa.pop("learn", None)
            sa.pop("learn_project", None)
        else:
            where = req.get("virtual") or req.get("abs") or ""
            asked = str(req.get("question", ""))
            q, args, ctx = before(where, asked, sa, req.get("project"))
            # the virtual path stays the user's file: ask_upload names evidence on the solved file by it
            r2 = {**req, "abs": ctx.get("abs") or req.get("abs"), "solver_args": args}
            ra = ctx.get("read_as")
            if ra and not req.get("solver") and _known_solver(ra["solver"]):
                r2["solver"] = ra["solver"]
                ctx["used"].append({"id": ra["id"], "kind": "sheet", "changed": True,
                                    "text": f"read as {ra['label']} (you chose it for this drawing)"})
    except Exception:  # noqa: BLE001 — fail closed: the request as it came, without our switch
        sa = {k: v for k, v in dict(req.get("solver_args") or {}).items() if k not in ("learn", "learn_project")}
        on = False
    if not on:
        return answer_fn({**req, "solver_args": sa})
    out = answer_fn({**r2, "question": q})  # the solver's own errors reach the caller as they would without us
    return after(where, asked, out, ctx, resolve=lambda q2: answer_fn({**r2, "question": q2}))


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
        evidence: list[dict] | None = None

        model_config = {"populate_by_name": True}

    def _view(path: str, project: str | None) -> dict:
        data = load(project_of(path, project))
        return {"project": data.get("project") or project_of(path, project), "rev": data.get("rev", 0),
                "items": list(reversed(data["items"])), "file": file_id(path),
                "kinds": list(KINDS), "on": enabled()}

    @router.get("")
    def list_corrections(path: str, project: str | None = None) -> dict:
        return _view(path, project)

    @router.post("")
    def add_correction(c: CorrectionIn) -> dict:
        payload = {k: v for k, v in c.model_dump().items() if v is not None and k not in ("path", "kind", "project", "is_")}
        if c.kind == "symbol":
            payload["is"] = True if c.is_ is None else bool(c.is_)
        try:
            item = add(c.path, c.kind, payload, c.project)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"item": item, **_view(c.path, c.project)}

    @router.post("/{item_id}/undo")
    def undo(item_id: str, path: str, project: str | None = None) -> dict:
        if set_active(path, item_id, False, project) is None:
            raise HTTPException(404, "no such correction")
        return _view(path, project)

    @router.post("/{item_id}/redo")
    def redo(item_id: str, path: str, project: str | None = None) -> dict:
        if set_active(path, item_id, True, project) is None:
            raise HTTPException(404, "no such correction")
        return _view(path, project)

    @router.delete("/{item_id}")
    def delete(item_id: str, path: str, project: str | None = None) -> dict:
        if not remove(path, item_id, project):
            raise HTTPException(404, "no such correction")
        return _view(path, project)

    class SimilarIn(BaseModel):
        path: str
        page: int | None = 0
        bbox: list[float]
        bbox_space: str | None = None
        exclude: list[list[float]] | None = None  # the answer's own boxes (already counted; they also give the size)
        label: str | None = None

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
