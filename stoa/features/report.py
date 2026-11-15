"""F8 'report' — the measurement report (captain 21:51, idea 5): one click on an answer -> a print-ready A4 page with the
drawing crop and the answer's outlines / markers, the formula, the scale and its source, the convention, assumptions,
solver / version, date and a 'Checked by' line (for claims and change-order disputes).

The page itself is built in the browser (web/src/features/report/index.ts) from the answer on screen. This module only
renders what the browser cannot do well at print quality:
  POST /feat/report/crop  {path, page, evidence: [Evidence…], mode: 'crop' | 'sheet', width?}  -> image/png
        crop  = the evidence's region (padded) at ~200 dpi of A4 width, outlines / measured lines / boxes drawn, numbered;
        sheet = the whole sheet small, with the crop's frame drawn on it (a locator: where on the sheet this is).
  GET  /feat/report/meta?path&page  -> {file, page, pages, sheet, title, version, generated_at}
Coordinates are the evidence space of docs/CONTRACT.md: pdf_pt of the UNROTATED page (rendered through the page's
rotation matrix) or image_px. Text and geometry only: no model calls, cost tier cheap.
"""
from __future__ import annotations

import io
import math
import os
import re
import subprocess
import time
from pathlib import Path

PAD_FRAC = 0.18           # padding around the evidence, as a share of its larger side
PAD_MIN_PT = 36.0         # at least half an inch of context on a PDF
PAD_MIN_PX = 60.0
TEAL = (13, 148, 136)
ORANGE = (234, 88, 12)
_RUN_NOTE = re.compile(r"\brun\b", re.I)
_OPENING_NOTE = re.compile(r"\b(?:opening|door|deduct)", re.I)


# ---------------------------------------------------------------------------------------------------- geometry
def _points(ev: dict) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for k in ("polygon", "polyline"):
        for p in ev.get(k) or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                pts.append((float(p[0]), float(p[1])))
    b = ev.get("bbox")
    if isinstance(b, (list, tuple)) and len(b) == 4:
        pts += [(float(b[0]), float(b[1])), (float(b[2]), float(b[3]))]
    return [p for p in pts if all(math.isfinite(v) for v in p)]


def _on_page(evidence: list[dict], page: int) -> list[dict]:
    return [e for e in evidence or [] if isinstance(e, dict) and int(e.get("page") or 0) == int(page) and _points(e)]


def region(evidence: list[dict], page: int, W: float, H: float, pad_min: float) -> tuple[float, float, float, float] | None:
    """The padded box around every located piece of evidence on this page, clamped to the page (source space)."""
    pts = [p for e in _on_page(evidence, page) for p in _points(e)]
    if not pts:
        return None
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    pad = min(max(pad_min, PAD_FRAC * max(x1 - x0, y1 - y0)), 2 * pad_min)   # a whole plan needs a margin, not a frame
    x0, y0, x1, y1 = max(0.0, x0 - pad), max(0.0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


# ---------------------------------------------------------------------------------------------------- rendering
def _font(px: int):
    from PIL import ImageFont

    for f in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _rgb(c) -> tuple[int, int, int] | None:
    """'#3b8fd0' -> (59, 143, 208); anything else -> None."""
    if isinstance(c, str) and len(c) == 7 and c[0] == "#":
        try:
            return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def _draw(img, evidence: list[dict], to_px, numbered: bool = True, frame: tuple | None = None):
    """Outlines (teal), measured / deducted lines (orange), boxes, numbered markers; frame = a locator rectangle."""
    from PIL import Image, ImageDraw

    base = img.convert("RGBA")
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    s = max(1.0, base.size[0] / 1600)
    lw = max(2, int(round(3 * s)))
    if frame is not None:
        a, b = to_px(frame[0], frame[1]), to_px(frame[2], frame[3])
        box = [min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])]
        d.rectangle(box, outline=ORANGE + (255,), width=max(3, lw))
    marks = []
    for i, e in enumerate(evidence):
        poly = [to_px(*p[:2]) for p in (e.get("polygon") or []) if len(p) >= 2]
        line = [to_px(*p[:2]) for p in (e.get("polyline") or []) if len(p) >= 2]
        b = e.get("bbox")
        if len(poly) >= 3:
            # a takeoff room keeps its room-type colour (F2's takeoff.TYPE_COLOR, the panel's legend); else teal
            c = _rgb(e.get("color")) or TEAL
            d.polygon(poly, fill=c + (70 if c != TEAL else 46,), outline=c + (255,))
            d.line(poly + [poly[0]], fill=c + (255,), width=lw)
        if len(line) >= 2:
            # an outline's measured line goes teal, and so does a separate piece of the same run ('Library: skirting run');
            # a lone line (an opening deducted, a measured wall) orange
            note = str(e.get("note") or "")
            run = len(poly) >= 3 or (bool(_RUN_NOTE.search(note)) and not _OPENING_NOTE.search(note))
            d.line(line, fill=(TEAL if run else ORANGE) + (255,), width=lw + (0 if run else 2))
        if len(poly) < 3 and len(line) < 2 and isinstance(b, (list, tuple)) and len(b) == 4:
            p0, p1 = to_px(b[0], b[1]), to_px(b[2], b[3])
            r = [min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1])]
            r = [r[0] - lw, r[1] - lw, r[2] + lw, r[3] + lw]
            d.rectangle(r, outline=TEAL + (255,), fill=TEAL + (30,), width=lw)
        if numbered and isinstance(b, (list, tuple)) and len(b) == 4:
            p0, p1 = to_px(b[0], b[1]), to_px(b[2], b[3])
            if e.get("label") is not None:     # a takeoff row number, centred on the room (once per label)
                if all(m[0] != e["label"] for m in marks):
                    marks.append((e["label"], (p0[0] + p1[0]) / 2 + 13 * s / 2, (p0[1] + p1[1]) / 2 + 13 * s / 2))
            else:
                marks.append((i + 1, min(p0[0], p1[0]), min(p0[1], p1[1])))
    img2 = Image.alpha_composite(base, ov)
    if marks:
        d2 = ImageDraw.Draw(img2)
        r = int(round(13 * s))
        f = _font(int(round(15 * s)))
        for n, x, y in marks[:160]:
            cx, cy = max(r + 1, x - r // 2), max(r + 1, y - r // 2)
            d2.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255), outline=TEAL + (255,), width=max(2, lw - 1))
            d2.text((cx, cy), str(n), fill=(17, 24, 39, 255), font=f, anchor="mm")
    return img2.convert("RGB")


def render(path: str, page: int, evidence: list[dict], mode: str = "crop", width: int = 1600) -> tuple[bytes, dict]:
    """PNG bytes + {box (source space), space, scale_px_per_unit} for the crop or the whole-sheet locator."""
    from PIL import Image

    page = int(page or 0)
    ev = _on_page(evidence, page)
    width = int(min(max(width, 200), 2400))
    if path.lower().endswith(".pdf"):
        import pymupdf as fitz

        with fitz.open(path) as doc:
            if not 0 <= page < doc.page_count:
                raise ValueError(f"page {page} out of range (the file has {doc.page_count})")
            pg = doc[page]
            M = pg.rotation_matrix
            W, H = float(pg.rect.width), float(pg.rect.height)
            if pg.rotation % 180:                      # evidence is in the UNROTATED page's points
                W, H = H, W
            box = region(ev, page, W, H, PAD_MIN_PT)
            whole = fitz.Rect(0, 0, W, H) * M
            if mode == "sheet" or box is None:
                clip = whole
            else:
                clip = (fitz.Rect(*box) * M) & whole
            zoom = min(width / max(clip.width, 1), 8.0)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            def to_px(x, y, _c=clip, _z=zoom, _M=M):
                q = fitz.Point(x, y) * _M
                return ((q.x - _c.x0) * _z, (q.y - _c.y0) * _z)

            space = "pdf_pt"
    else:
        with Image.open(path) as im0:
            im0.load()
            src_img = im0.convert("RGB")
        W, H = float(src_img.width), float(src_img.height)
        box = region(ev, page, W, H, max(PAD_MIN_PX, 0.02 * max(W, H)))
        crop = (0, 0, int(W), int(H)) if mode == "sheet" or box is None else tuple(int(round(v)) for v in box)
        img = src_img.crop(crop)
        zoom = min(width / max(img.width, 1), 4.0)
        if abs(zoom - 1) > 0.01:
            img = img.resize((max(1, int(img.width * zoom)), max(1, int(img.height * zoom))), Image.LANCZOS)

        def to_px(x, y, _c=crop, _z=zoom):
            return ((x - _c[0]) * _z, (y - _c[1]) * _z)

        space = "image_px"
    if mode == "sheet":
        img = _draw(img, ev, to_px, numbered=False, frame=box)
    else:
        img = _draw(img, ev, to_px, numbered=len(ev) > 1 or any(e.get("label") is not None for e in ev))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), {"box": list(box) if box else None, "space": space, "px_per_unit": round(zoom, 4),
                            "size": [img.width, img.height]}


# ---------------------------------------------------------------------------------------------------- meta
_VERSION: str | None = None


def version() -> str:
    """The code version the answer came from: the checkout's short commit (+ '-dirty'), read once."""
    global _VERSION
    if _VERSION is None:
        here = Path(__file__).resolve().parent
        try:
            sha = subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], cwd=here, capture_output=True, text=True,
                                 timeout=5).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=here,
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            _VERSION = (sha + ("-dirty" if dirty else "")) if sha else "unknown"
        except Exception:  # noqa: BLE001
            _VERSION = "unknown"
    return _VERSION


def meta(path: str, page: int, shown: str | None = None) -> dict:
    """{file, page, pages, sheet, title, version, generated_at}: the sheet number / title from the title block (xref)."""
    from stoa.features import xref

    out = {"file": Path(shown or path).name, "page": int(page), "pages": 1, "sheet": None, "title": None,
           "version": version(), "generated_at": time.strftime("%Y-%m-%d %H:%M %Z")}
    try:
        idx = xref.sheet_index(path)
        out["pages"] = max(1, len(idx))
        e = next((e for e in idx if e.page == int(page)), None)
        if e:
            out["sheet"], out["title"] = e.number, e.title
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------------------------------------------- takeoff
def takeoff_result(path: str, page: int) -> dict | None:
    """F2's FINISHED takeoff of this sheet page from its disk cache (stoa.features.takeoff job_key / cache_dir), or None.
    Read-only: the report never starts a takeoff job (CPU budget); the visitor runs the takeoff from its own panel."""
    try:
        from stoa.features import takeoff as tk

        f = tk.cache_dir() / tk.job_key(path, int(page)) / "result.json"
        if not f.exists():
            return None
        import json

        r = json.loads(f.read_text())
        return r if r.get("status") == "done" and r.get("rows") else None
    except Exception:  # noqa: BLE001
        return None


_VISITOR_RX = r"[A-Za-z0-9_-]{8,64}"


def takeoff_table(path: str, page: int, shown: str | None = None, visitor: str | None = None) -> dict | None:
    """The takeoff AS ITS PANEL SHOWS IT: F2's status() = finish() of the stored rows with the visitor's own Measure fixes,
    room types, the review queue and the totals recomputed without it (UX 23:35: the report must not disagree with the
    panel). F2 23:52: fixes are kept PER VISITOR (the browser's 'stoa.visitor' id): passed through when takeoff.status()
    takes it, so the report shows this person's Measure fixes and nobody else's. Falls back to the stored result (an older
    takeoff module); None while the job is not finished."""
    try:
        import inspect
        import re

        from stoa.features import takeoff as tk

        kw: dict = {"virtual": shown}
        if "visitor" in inspect.signature(tk.status).parameters:
            kw["visitor"] = visitor if visitor and re.fullmatch(_VISITOR_RX, visitor) else None
        st = tk.status(tk.job_key(path, int(page)), **kw)
        if st.get("status") == "done" and st.get("rows"):
            return st
    except Exception:  # noqa: BLE001
        pass
    return takeoff_result(path, page)


def _types() -> tuple[dict, dict, list]:
    """F2's room-type taxonomy (takeoff.ROOM_TYPES): label + colour per key, in legend order; empty when unavailable."""
    try:
        from stoa.features import takeoff as tk

        order = [k for k, *_ in tk.ROOM_TYPES] + [tk.OTHER_TYPE[0]]
        return dict(tk.TYPE_LABEL), dict(tk.TYPE_COLOR), order
    except Exception:  # noqa: BLE001
        return {}, {}, []


def takeoff_rows(path: str, page: int, shown: str | None = None, visitor: str | None = None) -> dict:
    """{available, rows: [{n, section, item, copy, copies, quantity, unit, value, confidence, method, assumptions,
    review, room_type, type_label, type_color, evidence}], types, scale_n, source, rooms}: the takeoff's rows for the
    report, room rows numbered 1..k in sheet order, their evidence re-pointed at the visitor's own path (the cache may
    hold another upload's virtual path) and coloured by room type."""
    r = takeoff_table(path, page, shown, visitor)
    if not r:
        return {"available": False, "rows": []}
    labels, colors, order = _types()
    num: dict[tuple, int] = {}
    out = []
    for x in r.get("rows") or []:
        n = None
        if x.get("section") == "room":
            k = (x.get("item"), x.get("copy"))
            n = num.setdefault(k, len(num) + 1)
        # a sheet's evidence = boxes / outlines on this page; an IFC model's (F2 23:14: kind 'ifc model') = GlobalIds
        rt = x.get("room_type")
        if rt is None and x.get("section") == "room" and labels:
            try:
                from stoa.features import takeoff as tk

                rt = tk.room_type(x.get("item") or "")
            except Exception:  # noqa: BLE001
                rt = None
        col = colors.get(rt) if rt else None
        ev = [dict(e, file=shown or e.get("file"), label=n, **({"color": col} if col else {}))
              for e in (x.get("evidence") or [])
              if (e.get("bbox") and int(e.get("page") or 0) == int(page)) or (e.get("ifc_guid") and not e.get("bbox"))][:4]
        out.append({"n": n, **{k: x.get(k) for k in ("section", "item", "copy", "copies", "quantity", "unit", "value",
                                                       "confidence", "method", "assumptions", "storey", "ifc_guid",
                                                       "review", "fixed", "derived", "leaves")},
                    "room_type": rt, "type_label": labels.get(rt) if rt else None, "type_color": col, "evidence": ev})
    return {"available": True, "rows": out, "scale_n": r.get("scale_n"), "source": r.get("source"),
            "rooms": r.get("rooms"), "engine": r.get("engine"), "kind": r.get("kind"),
            "types": [{"type": k, "label": labels.get(k), "color": colors.get(k)} for k in order]}


# ---------------------------------------------------------------------------------------------------- HTTP
def enabled() -> bool:
    try:
        from stoa.features import enabled as _en

        return "report" in _en()
    except Exception:  # noqa: BLE001
        v = os.environ.get("STOA_FEATURES", "")
        return "report" in v.split(",") or v.strip() == "all"


def _make_router():
    from fastapi import APIRouter, Body, HTTPException, Query
    from fastapi.responses import Response

    from stoa.features.xref import resolve_path

    router = APIRouter(prefix="/feat/report", tags=["feat:report"])

    def _path(p: str) -> str:
        if not enabled():
            raise HTTPException(404, "feature 'report' is off (STOA_FEATURES)")
        try:
            return resolve_path(p)
        except FileNotFoundError:
            raise HTTPException(404, "no such file") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @router.get("/health")
    def health():
        return {"ok": True, "enabled": enabled(), "version": version()}

    @router.get("/meta")
    def get_meta(path: str = Query(...), page: int = Query(0, ge=0)):
        return meta(_path(path), page, shown=path)

    @router.get("/takeoff")
    def get_takeoff(path: str = Query(...), page: int = Query(0, ge=0), visitor: str = ""):
        return takeoff_rows(_path(path), page, shown=path, visitor=visitor or None)

    @router.post("/crop")
    def crop(body: dict = Body(...)):
        f = _path(str(body.get("path") or ""))
        mode = "sheet" if body.get("mode") == "sheet" else "crop"
        ev = body.get("evidence") or []
        if not isinstance(ev, list) or len(ev) > 400:
            raise HTTPException(400, "evidence must be a list (at most 400 items)")
        try:
            png, info = render(f, int(body.get("page") or 0), ev, mode=mode, width=int(body.get("width") or 1600))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return Response(png, media_type="image/png", headers={"X-Report-Crop": str(info["box"]), "Cache-Control": "no-store"})

    return router


try:
    router = _make_router()
except Exception:  # noqa: BLE001  (fastapi missing: the module still imports; the registry fails closed)
    router = None
