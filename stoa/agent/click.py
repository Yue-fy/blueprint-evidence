"""What the visitor pointed at (captain 19:47 / 19:54): a right-click POINT (W5's menu), a drawn REGION (F1's box / lasso)
or a VIEW title, sent with /ask as `context: {point?: [x, y], region?: [[x, y], ...], view?: str, space: 'pdf_pt' |
'image_px', page?: int}` in the same space as Evidence.bbox (PDF: PyMuPDF page points, y down; image: pixels of the
uploaded file). It removes the ambiguity of unseen sheets ('this room', 'in here', 'what is this?').

Delivery: `with use(ctx): solver.solve(path, q)` (contextvar), or $STOA_ASK_CONTEXT (JSON, W5's upload worker sets it per
question; P1's stoa.formats.normalize.ask_context_on rewrites it into the SOLVED copy's coordinates around the upload
solver's read, with 'dropped' saying why a point could not be mapped). The pipeline calls `answer(...)` once per
question (nested solves see no context), after the page pick, on the file it actually reads. `mapped(prep)` does the
same for the contextvar path (not wired: P1's env mapping is the one in upload.py).

What it does (every answer says so in one 'context: …' trace line and one assumption):
- REGION + count     -> the reader's answer, keeping only its per-item evidence marks whose centre is inside the polygon
                        (only when the reader gave exactly one mark per counted item; else the count is kept, said so).
- REGION + area      -> 'rooms in here': the printed room tags inside, summed; 'this region / area I drew': the polygon's
                        area at the sheet's scale (evidence = the polygon); neither said: tags inside if any, else polygon.
- REGION + length    -> 2 points: the distance at scale; a polygon: its long side (oriented extent), approximate.
- REGION + other     -> the room tags / labels inside, listed.
- POINT  + 'what is here / this?' -> the room tag or printed label under / nearest the point.
- POINT  + 'this room' (area / length / count) -> the room under the point: traced outline at the point (E1 linework on
                        vector PDFs, O1 raster fill on pictures), its printed tag inside the outline (exact) or nearest;
                        other questions get the room's NAME in place of 'this room' and run the normal cascade.
- VIEW               -> the view's title appended to the question (VP's scope_for picks it).
Nothing here calls a hosted model.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import re
from dataclasses import dataclass, field

from stoa.contract import Answer, Evidence

_CTX: contextvars.ContextVar = contextvars.ContextVar("stoa_ask_context", default=None)
_DONE = "done"  # set while the context's answer runs: nested solves (rewritten questions) see none


@dataclass
class Ask:
    point: tuple | None = None
    region: list = field(default_factory=list)
    view: str | None = None
    space: str = "pdf_pt"
    page: int | None = None
    notes: list = field(default_factory=list)

    @classmethod
    def parse(cls, d) -> "Ask | None":
        if isinstance(d, Ask):
            return d
        if isinstance(d, (str, bytes)):
            try:
                d = json.loads(d)
            except Exception:  # noqa: BLE001
                return None
        if not isinstance(d, dict):
            return None
        if isinstance(d.get("context"), dict):
            d = {**{k: v for k, v in d.items() if k != "context"}, **d["context"]}
        pt, reg = d.get("point"), d.get("region")
        try:
            point = (float(pt[0]), float(pt[1])) if pt is not None and len(pt) >= 2 else None
        except Exception:  # noqa: BLE001
            point = None
        region = []
        try:
            for p in reg or []:
                region.append((float(p[0]), float(p[1])))
        except Exception:  # noqa: BLE001
            region = []
        if len(region) == 4 and not isinstance((reg or [None])[0], (list, tuple)):
            region = []
        if not region and isinstance(reg, (list, tuple)) and len(reg) == 4 and all(isinstance(v, (int, float))
                                                                                   for v in reg):
            x0, y0, x1, y1 = (float(v) for v in reg)  # a box [x0, y0, x1, y1]
            region = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        view = str(d.get("view")).strip() if d.get("view") else None
        space = str(d.get("space") or "pdf_pt")
        if space not in ("pdf_pt", "image_px"):
            return None
        try:
            page = int(d["page"]) if d.get("page") is not None else None
        except Exception:  # noqa: BLE001
            page = None
        dropped = [str(x) for x in (d.get("dropped") or [])]  # P1's normalize.ask_context_on: why a point was not mapped
        if point is None and len(region) < 2 and not view:
            return None
        return cls(point=point, region=region, view=view, space=space, page=page,
                   notes=[f"not used: {x}" for x in dropped])

    def line(self) -> str:
        parts = []
        if self.point:
            parts.append(f"point ({self.point[0]:.1f}, {self.point[1]:.1f})")
        if len(self.region) == 2:
            parts.append("a line of 2 points")
        elif self.region:
            parts.append(f"a region of {len(self.region)} points")
        if self.view:
            parts.append(f"view '{self.view}'")
        return " + ".join(parts) + f" in {self.space}" + (f" on page {self.page + 1}" if self.page is not None else "")

    def scaled(self, f: float, space: str | None = None) -> "Ask":
        return Ask(point=(self.point[0] * f, self.point[1] * f) if self.point else None,
                   region=[(x * f, y * f) for x, y in self.region], view=self.view, space=space or self.space,
                   page=self.page, notes=list(self.notes))


@contextlib.contextmanager
def use(ctx):
    """The context for the questions asked inside the block (None / {} = none, also not from the environment)."""
    tok = _CTX.set(Ask.parse(ctx) or _DONE)
    try:
        yield
    finally:
        _CTX.reset(tok)


def current() -> Ask | None:
    v = _CTX.get()
    if v == _DONE:
        return None
    if isinstance(v, Ask):
        return v
    raw = os.environ.get("STOA_ASK_CONTEXT")
    return Ask.parse(raw) if raw else None


@contextlib.contextmanager
def mapped(prep):
    """Carry the context through the upload normaliser (stoa.formats.normalize.Prepared): the solved copy's space."""
    ctx = current()
    if ctx is None or prep is None:
        yield
        return
    new = ctx
    try:
        if getattr(prep, "photo_h", None):
            new = None  # a deskewed photo: the point is not mapped (said in the trace by the caller)
        elif getattr(prep, "scan_zoom", 0) and ctx.space == "pdf_pt":
            new = ctx.scaled(float(prep.scan_zoom), "image_px")
        elif getattr(prep, "img_scale", 0) and ctx.space == "image_px":
            new = ctx.scaled(float(prep.img_scale))
        elif getattr(prep, "paper_f", 0) and ctx.space == "pdf_pt":
            new = ctx.scaled(float(prep.paper_f))
        if new is not None and ctx.page is not None and getattr(prep, "pages", 1) > 1 and \
                int(getattr(prep, "page", 0)) != ctx.page:
            new = None  # the context was drawn on another page than the one read
        if new is not None and new is not ctx:
            new.page = 0 if ctx.page is not None else None
    except Exception:  # noqa: BLE001
        new = ctx
    tok = _CTX.set(new or _DONE)
    try:
        yield
    finally:
        _CTX.reset(tok)


# -- geometry --------------------------------------------------------------------------------------------------------
def inside(pt, poly) -> bool:
    x, y = pt
    res = False
    for (x0, y0), (x1, y1) in zip(poly, list(poly[1:]) + [poly[0]]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / ((y1 - y0) or 1e-12):
            res = not res
    return res


def poly_area(poly) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(poly, list(poly[1:]) + [poly[0]]))) / 2


def centre(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def long_side(poly) -> float:
    """The longer side of the polygon's minimum-area enclosing rectangle (rotating calipers over hull edges)."""
    pts = sorted(set(poly))
    if len(pts) < 3:
        return math.dist(pts[0], pts[-1]) if len(pts) == 2 else 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo, hi = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    hull = lo[:-1] + hi[:-1]
    best = None
    for a, b in zip(hull, hull[1:] + hull[:1]):
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        c, s = math.cos(-ang), math.sin(-ang)
        xs = [p[0] * c - p[1] * s for p in hull]
        ys = [p[0] * s + p[1] * c for p in hull]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if best is None or w * h < best[0]:
            best = (w * h, max(w, h))
    return best[1] if best else 0.0


# -- what is printed where -------------------------------------------------------------------------------------------
def _abs(path: str) -> str:
    from stoa.data import abspath

    return str(abspath(path))


def _is_pdf(path: str) -> bool:
    return str(path).lower().endswith(".pdf")


def _layer(p: str):
    if _is_pdf(p):
        return None
    try:
        from stoa.solvers.area_tags import _cached_layer

        return _cached_layer(p)
    except Exception:  # noqa: BLE001
        return None


def tags(path: str, page: int = 0) -> list:
    """VP's room tags (name / number / printed area + box) in Evidence space."""
    p = _abs(path)
    try:
        from stoa.sheet.labels import room_tags

        lay = _layer(p)
        if not _is_pdf(p) and lay is None:
            return []
        return room_tags(p, page, layer=lay)
    except Exception:  # noqa: BLE001
        return []


def lines(path: str, page: int = 0) -> list[tuple[str, tuple]]:
    """(text, box) of every printed line in Evidence space: PDF words grouped by line, or the image's text layer."""
    p = _abs(path)
    if _is_pdf(p):
        try:
            import pymupdf

            with pymupdf.open(p) as doc:
                ws = doc[page].get_text("words")
        except Exception:  # noqa: BLE001
            return []
        groups: dict = {}
        for w in ws:
            groups.setdefault((w[5], w[6]), []).append(w)
        out = []
        for g in groups.values():
            g.sort(key=lambda w: w[0])
            out.append((" ".join(w[4] for w in g), (min(w[0] for w in g), min(w[1] for w in g),
                                                   max(w[2] for w in g), max(w[3] for w in g))))
        return out
    try:
        from stoa.solvers.label_count import _image_lines

        return [(str(t), tuple(bb)) for t, bb in (_image_lines(p) or ())]
    except Exception:  # noqa: BLE001
        return []


_NAMEISH = re.compile(r"[A-Za-zÄÖÅäöåÜüß]{3,}")
_NOT_NAME = re.compile(r"\b(scale|sheet|drawing|note|see|refer|dwg|rev|date|plan|elevation|section|detail|typ\.?|"
                       r"ceiling|finish|level|grid)\b|^\s*[\d'\".,x×\- /:]+\s*$", re.I)


def _dist_box(pt, box) -> float:
    dx = max(box[0] - pt[0], 0, pt[0] - box[2])
    dy = max(box[1] - pt[1], 0, pt[1] - box[3])
    return math.hypot(dx, dy)


def _diag(path: str) -> float:
    p = _abs(path)
    try:
        if _is_pdf(p):
            import pymupdf

            with pymupdf.open(p) as doc:
                r = doc[0].rect
                return math.hypot(r.width, r.height)
        from PIL import Image

        with Image.open(p) as im:
            return math.hypot(*im.size)
    except Exception:  # noqa: BLE001
        return 1000.0


def _scale(pipe, path: str, ctx: Ask, at=None) -> tuple[float, str] | None:
    """Metres per Evidence unit at a spot, and where the scale came from."""
    p = _abs(path)
    if ctx.space == "pdf_pt" and _is_pdf(p):
        n_user = getattr(pipe, "user_scale", None)
        if n_user:
            return 0.0254 / 72 * float(n_user), f"1:{n_user:g} (set by you)"
        try:
            from stoa.sheet import views

            vs = views(p, 0)
            if at is not None:
                hold = [v for v in vs if v.contains(at[0], at[1], pad=2) and (v.page_ratio or v.ratio)]
                if hold:
                    v = hold[0]
                    n = float(v.page_ratio or v.ratio)
                    return 0.0254 / 72 * n, f"1:{n:g} printed for view '{v.title}'"
            rs = {float(v.page_ratio or v.ratio) for v in vs if (v.page_ratio or v.ratio)}
            if len(rs) == 1:
                n = rs.pop()
                return 0.0254 / 72 * n, f"1:{n:g} printed on the sheet"
        except Exception:  # noqa: BLE001
            pass
        try:
            from stoa.agent.vision import _pdf_scale

            sc = _pdf_scale(p)
            if sc:
                return 0.0254 / 72 * sc[0], f"1:{sc[0]:g} {sc[1]}"
        except Exception:  # noqa: BLE001
            pass
        return None
    if ctx.space == "image_px" and not _is_pdf(p):
        ppm = getattr(pipe, "px_per_m", None)
        if ppm:
            return 1.0 / float(ppm), f"{float(ppm):.1f} px per metre (set by you)"
        try:
            from stoa.agent.vision import _image_bar
            from stoa.solvers.raster import scalebar as SB

            bar = _image_bar(p)
            if bar is not None and bar.px_per_m:
                return 1.0 / float(bar.px_per_m), SB.scale_source(bar)
        except Exception:  # noqa: BLE001
            pass
    return None


def _sqft(question: str, tg: list) -> bool:
    if re.search(r"\b(sq\.? ?ft|square f(ee|oo)t|ft²|ft2|sf)\b", question, re.I):
        return True
    if re.search(r"\b(m²|m2|sq\.? ?m|square met(er|re)s?)\b", question, re.I):
        return False
    units = [t.unit for t in tg]
    return bool(units) and units.count("ft2") > len(units) / 2


# -- question shapes -------------------------------------------------------------------------------------------------
_WHAT_HERE = re.compile(r"^\s*(what|which)(?:'s| is| are)?\s+(?:is\s+)?(?:at\s+)?(this|here|that|these|the (?:point|spot|"
                        r"thing|symbol|room|space|area|label|item|object)s?\s+(?:i|you)?\s*(?:clicked|selected|marked|"
                        r"pointed|picked)|the (?:point|spot)|in (?:this|the) (?:region|area|box|selection))|^\s*(identify|"
                        r"name|describe) (this|that|it|the)\b|what am i looking at|\bwhat(?:'s| is) (?:this|that|here)\b",
                        re.I)
_THIS_ROOM = re.compile(r"\b(this|that|the (?:selected|clicked|marked))\s+(room|space|office|area|unit|apartment|flat|"
                        r"zone|bedroom|bathroom|kitchen|toilet|classroom|corridor|hall|store|storage)\b|\b(here|the "
                        r"room (?:i|you) (?:clicked|selected|marked|pointed at))\b", re.I)
_ROOMS_IN = re.compile(r"\b(rooms?|spaces?|offices?|units?|apartments?)\b.{0,40}\b(in (?:here|this|the (?:region|area|"
                       r"box|selection|outline))|inside|within|selected|marked|enclosed)\b|\b(in (?:here|this region|"
                       r"this box|the selection))\b.{0,40}\brooms?\b", re.I)
_REGION_AREA = re.compile(r"\b(this|the|selected|marked|drawn|outlined)\s+(region|polygon|shape|outline|selection|box|"
                          r"zone|area i (?:drew|marked|selected))\b|\barea (?:of|in) (?:this|the) (?:region|selection|"
                          r"box|polygon|outline)\b|\bthis area\b", re.I)
_SCOPE_WORDS = re.compile(r"\s*\b(?:in|inside|within)\s+(?:here|this (?:region|area|box|selection|zone|part|view|plan)|"
                          r"the (?:selected|marked|drawn) (?:region|area|box|part))\b|\s*\bhere\b(?=\s*\?*\s*$)", re.I)
_LIKE_THIS = re.compile(r"\b(like this|like that|of these|of this (?:kind|type|symbol)|this symbol|these symbols|"
                        r"similar|the same (?:symbol|kind|type))\b", re.I)


def _strip_scope(q: str) -> str:
    out = _SCOPE_WORDS.sub("", q).strip()
    out = re.sub(r"\s+\?", "?", out)
    out = re.sub(r"\b(are|is)\?$", r"\1 there?", out)  # 'How many doors are in here?' -> 'How many doors are there?'
    return out or q


_NOTE_AREA = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(m²|m2|ft²|ft2|sq\.? ?ft|sf)\b", re.I)


def _at_point(ans: Answer, pt, space: str) -> tuple[float, Evidence] | None:
    """A reader that measured SEVERAL rooms of the asked name (plan_rooms: '6 rooms are labelled Secondary Classroom
    … their mean') and drew one outline per room: the value of the one outline that contains the point."""
    per = []
    for e in ans.evidence:
        if e.bbox_space != space or not e.polygon or len(e.polygon) < 3:
            continue
        m = _NOTE_AREA.search(e.note or "")
        if m:
            u = "m2" if m.group(2).lower() in ("m²", "m2") else "ft2"
            per.append((float(m.group(1).replace(",", "")), u, e))
    if len(per) < 2:
        return None
    hit = [x for x in per if inside(pt, x[2].polygon)]
    if len(hit) != 1:
        return None
    v, u, e = hit[0]
    want = str(ans.unit or "").lower().replace("²", "2")
    if want in ("m2", "ft2") and u != want:
        v = v * 0.3048 ** 2 if u == "ft2" else v / 0.3048 ** 2
    return round(v, 2), e


def _f1_region(path: str) -> bool:
    """F1's region feature is on (STOA_FEATURES has 'region'), or this is its masked copy of the sheet."""
    feats = {f.strip().lower() for f in os.environ.get("STOA_FEATURES", "").split(",")}
    return "region" in feats or ".region-" in str(path)


def _with_view(q: str, view: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", view.lower()) if w not in ("plan", "floor", "the")]
    if words and all(w in q.lower() for w in words):
        return q
    return q.rstrip(" ?") + f" in {view}?"


# -- the answer ------------------------------------------------------------------------------------------------------
def _note(ctx: Ask, what: str) -> str:
    return f"scoped to the {what} you marked ({ctx.line()})"


def _with_ctx(ans: Answer, ctx: Ask, what: str, line: str) -> Answer:
    ans.trace = [f"context: {line}", *(ans.trace or [])]
    note = _note(ctx, what)
    if note not in ans.assumptions:
        ans.assumptions = [*ans.assumptions, note]
    return ans


def _ev(path, ctx, box, note, exact, polygon=None, polyline=None) -> Evidence:
    kw = {}
    if polygon:
        kw["polygon"] = [[round(x, 2), round(y, 2)] for x, y in polygon]
    if polyline:
        kw["polyline"] = [[round(x, 2), round(y, 2)] for x, y in polyline]
    return Evidence(file=path, page=0 if _is_pdf(path) else None, bbox=[round(v, 2) for v in box],
                    bbox_space=ctx.space, note=note, exact=exact, **kw)


def _bbox(pts) -> list:
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def answer(pipe, path: str, question: str, ctx: Ask, solve) -> Answer:
    """The question answered within what the visitor marked. `solve(path, q)` = the pipeline's own cascade."""
    from stoa.router.intent import parse

    q = _with_view(question, ctx.view) if ctx.view else question
    task = parse(q).task
    if len(ctx.region) >= 2 and _f1_region(path):
        # F1 20:32: F1's region feature scopes regions OUTSIDE the pipeline (masked '.region-' copy for counts, per-room
        # answers on the full sheet for areas): never a second region answer in here (its per-room asks would come
        # back as a region sum)
        ctx.notes.append("region left to the region feature (F1)")
        ctx.region = []
    if len(ctx.region) >= 2:
        try:
            a = _region(pipe, path, q, ctx, task, solve)
        except Exception as e:  # noqa: BLE001 - never lose the answer for the context
            a = None
            ctx.notes.append(f"region not applied ({type(e).__name__}: {e})")
        if a is not None:
            return a
    if ctx.point is not None:
        try:
            a = _point(pipe, path, q, ctx, task, solve)
        except Exception as e:  # noqa: BLE001
            a = None
            ctx.notes.append(f"point not applied ({type(e).__name__}: {e})")
        if a is not None:
            return a
    ans = solve(path, q)
    why = "; ".join(ctx.notes) or ("the view named in the question" if ctx.view and q != question else
                                   "the question names what it means; answered as asked")
    ans.trace = [f"context: {ctx.line()} — {why}" + (f"; asked as '{q}'" if q != question else ""), *(ans.trace or [])]
    return ans


_ONE_ROOM_Q = re.compile(r"\b(?:the|this|that)\s+(?:room|space)\b(?!s)", re.I)


def _one_room_label(path: str, poly, q: str) -> str | None:
    """The one room NAME printed inside the drawn region, when the question asks about 'the room' (singular); None for
    zero or several names (then the region's own area / its tags answer as before)."""
    if os.environ.get("STOA_G1_ONE_ROOM", "1") == "0" or not _ONE_ROOM_Q.search(q):
        return None
    got = sorted(((t.strip(), bb) for t, bb in lines(path, 0) if inside(centre(bb), poly) and _NAMEISH.search(t) and
                  not _NOT_NAME.search(t) and len(t.split()) <= 4 and not re.search(r"\d", t)), key=lambda x: x[1][1])
    merged: list[list] = []
    for t, bb in got:  # a label printed on two lines ('Multi-Purpose' / 'Hall') is one name
        if merged:
            pt, pb = merged[-1]
            h = max(1.0, pb[3] - pb[1])
            if 0 <= bb[1] - pb[3] <= 0.8 * h and min(bb[2], pb[2]) - max(bb[0], pb[0]) > 0:
                merged[-1] = [f"{pt} {t}", (min(pb[0], bb[0]), pb[1], max(pb[2], bb[2]), bb[3])]
                continue
        merged.append([t, bb])
    names = list(dict.fromkeys(t for t, _ in merged))
    return names[0] if len(names) == 1 else None


def _region(pipe, path, q, ctx, task, solve) -> Answer | None:
    poly = ctx.region
    if len(poly) == 2 or (task == "length" and not _ROOMS_IN.search(q)):
        sc = _scale(pipe, path, ctx, centre(_bbox(poly)))
        if sc is None:
            a = Answer(value=None, confidence=0.0, solver="pipeline/context",
                       evidence=[_ev(path, ctx, _bbox(poly), "the line / region you drew", None,
                                     polyline=poly if len(poly) == 2 else None, polygon=poly if len(poly) > 2 else None)],
                       trace=["the line / region you drew could not be measured: no scale found on this drawing"],
                       missing=["the drawing's scale: set it (two points and their real distance) and ask again"])
            return _with_ctx(a, ctx, "line" if len(poly) == 2 else "region", ctx.line())
        mpu, src = sc
        if len(poly) == 2:
            d, how, conf = math.dist(*poly) * mpu, "the distance between the two points you marked", 0.7
        else:
            d, how, conf = long_side(poly) * mpu, "the long side of the region you drew (its oriented extent)", 0.4
        ft = bool(re.search(r"\b(feet|foot|ft)\b", q, re.I))
        v, unit = (round(d / 0.3048, 2), "ft") if ft else (round(d, 3), "m")
        ev = [_ev(path, ctx, _bbox(poly), f"{how}: {v} {unit} at {src}", False,
                  polyline=poly if len(poly) == 2 else None, polygon=poly if len(poly) > 2 else None)]
        a = Answer(value=v, unit=unit, confidence=conf, evidence=ev, solver="pipeline/context",
                   trace=[f"{how} = {v} {unit} at {src}"],
                   assumptions=[f"scale: {src}", "≈ ±3 % (clicking accuracy of the marked points; zoom in for a "
                                                 "tighter reading)"],  # T1 21:16, Ziying xr-19
                   missing=["a dimension string for an exact value"])
        return _with_ctx(a, ctx, "line" if len(poly) == 2 else "region", ctx.line())
    tg = [t for t in tags(path, 0) if inside(centre(t.box), poly)]
    if task == "count":
        q2 = _strip_scope(q)
        base = solve(path, q2)
        return _filter_count(base, path, q2, ctx)
    if task == "area":
        want_rooms = bool(_ROOMS_IN.search(q)) or (bool(tg) and not _REGION_AREA.search(q))
        if want_rooms and tg:
            sqft = _sqft(q, tg)
            k = {("ft2", True): 1.0, ("m2", False): 1.0, ("ft2", False): 0.3048 ** 2, ("m2", True): 1 / 0.3048 ** 2}
            vals = [t.value * k[(t.unit, sqft)] for t in tg]
            tot = round(sum(vals), 2)
            unit = "ft2" if sqft else "m2"
            terms = " + ".join(f"{t.label} {t.text}" for t in tg)
            ev = [_ev(path, ctx, t.box, f"{t.label}: {t.text} (printed)", True) for t in tg]
            ev.append(_ev(path, ctx, _bbox(poly), "the region you drew", None, polygon=poly))
            a = Answer(value=tot, unit=unit, confidence=0.8 if len(tg) > 1 else 0.85, evidence=ev,
                       solver="pipeline/context", trace=[f"{len(tg)} printed room tag(s) inside the region you drew: "
                                                         f"{terms} = {tot} {unit}"],
                       assumptions=["rooms = the printed room tags whose centre lies inside the region"],
                       missing=[])
            return _with_ctx(a, ctx, "region", ctx.line())
        one = _one_room_label(path, poly, q) if want_rooms and not tg else None
        if one:
            # UX M23 (DfE L00, box over the Multi-Purpose Hall, 'Area of the room in here'): the box's own area
            # (287.87 'rough guess') while the hall's measured outline is 247.73: one labelled room inside the box = that
            # room's own measured area, named
            q1 = f"What is the area of the {one}?"
            try:  # the full solver (an upload: its traced vector copy measures the room), without the region
                with use(None):
                    a = pipe.solve(path, q1) if hasattr(pipe, "solve") else solve(path, q1)
            except Exception:  # noqa: BLE001
                a = solve(path, q1)
            if a.value is not None and isinstance(a.value, (int, float)) and a.confidence >= 0.3 and \
                    any(e.bbox or e.polygon for e in a.evidence or []):
                a = a.model_copy(deep=True)
                a.trace = [f"1 room in your box: {one} -> its own measured area", *a.trace]
                a.assumptions = [f"1 room in your box: {one} (its own outline, not the box)", *a.assumptions]
                return _with_ctx(a, ctx, "region", ctx.line())
        sc = _scale(pipe, path, ctx, centre(_bbox(poly)))
        if sc is None:  # never the WHOLE sheet's area for 'this region' (AFCEC PNG: the floor total 26,562 SF)
            a = Answer(value=None, confidence=0.0, solver="pipeline/context",
                       evidence=[_ev(path, ctx, _bbox(poly), "the region you drew", None, polygon=poly)],
                       trace=["the region you drew could not be measured: no scale found on this drawing"],
                       missing=["the drawing's scale: set it (two points and their real distance) and ask again"])
            return _with_ctx(a, ctx, "region", ctx.line())
        mpu, src = sc
        m2 = poly_area(poly) * mpu * mpu
        sqft = _sqft(q, tg)
        v, unit = (round(m2 / 0.3048 ** 2, 2), "ft2") if sqft else (round(m2, 2), "m2")
        ev = [_ev(path, ctx, _bbox(poly), f"the region you drew: {v} {'ft²' if sqft else 'm²'} at {src}", False,
                  polygon=poly)]
        a = Answer(value=v, unit=unit, confidence=0.6, evidence=ev, solver="pipeline/context",
                   trace=[f"area of the region you drew ({len(poly)} points) = {v} {unit} at {src}"],
                   assumptions=[f"scale: {src}", "the area is the polygon you drew, as drawn (not a room outline)",
                                "≈ ±3 % (clicking accuracy of a hand-drawn outline)"],
                   missing=["printed room areas for an exact value"] if not tg else [])
        return _with_ctx(a, ctx, "region", ctx.line())
    if task == "text" or _WHAT_HERE.search(q):
        names = [t.label + (f" ({t.text})" if t.text else "") for t in tg]
        if not names:
            names = [t for t, bb in lines(path, 0) if inside(centre(bb), poly) and _NAMEISH.search(t)
                     and not _NOT_NAME.search(t)][:12]
        if not names:
            return None
        ev = [_ev(path, ctx, t.box, t.label, True) for t in tg] or []
        ev.append(_ev(path, ctx, _bbox(poly), "the region you drew", None, polygon=poly))
        a = Answer(value="; ".join(names), unit=None, confidence=0.7, evidence=ev, solver="pipeline/context",
                   trace=[f"printed inside the region you drew: {'; '.join(names)}"])
        return _with_ctx(a, ctx, "region", ctx.line())
    return None


def _filter_count(base: Answer, path: str, q2: str, ctx: Ask) -> Answer:
    poly = ctx.region
    v = base.value if isinstance(base.value, (int, float)) and not isinstance(base.value, bool) else None
    marks = [e for e in base.evidence if e.bbox and e.bbox_space == ctx.space and (e.page in (None, 0))
             and not e.polygon and (e.bbox[2] - e.bbox[0]) * (e.bbox[3] - e.bbox[1]) >= 0]
    if v is not None and marks and len(marks) == int(v):
        keep = [e for e in marks if inside(centre(e.bbox), poly)]
        n = len(keep)
        other = [e for e in base.evidence if e not in marks]
        a = base.model_copy(update={
            "value": n, "evidence": keep + other[:3] + [_ev(path, ctx, _bbox(poly), "the region you drew", None,
                                                            polygon=poly)],
            "confidence": round(float(base.confidence or 0) * (1.0 if n else 0.9), 3),
            "solver": base.solver, "trace": [*base.trace, f"region: {n} of the reader's {int(v)} marked items lie "
                                                          f"inside the region you drew"]})
        return _with_ctx(a, ctx, "region", f"{ctx.line()}; asked the sheet '{q2}', kept the items inside")
    why = ("the reader gave no per-item marks" if not marks else
           f"the reader's {len(marks)} marks are not one per counted item ({base.value})") if v is not None else \
        "no count was read"
    base.confidence = round(float(base.confidence or 0) * 0.5, 3)
    base.missing = [*base.missing, f"the count could not be limited to the region you drew ({why}): it is the count "
                                   "for the whole view"]
    return _with_ctx(base, ctx, "region", f"{ctx.line()}; NOT applied: {why}")


def _point(pipe, path, q, ctx, task, solve) -> Answer | None:
    pt = ctx.point
    if _WHAT_HERE.search(q):
        return _what_here(pipe, path, q, ctx)
    if task == "count" and _LIKE_THIS.search(q):
        a = count_like(pipe, path, q, ctx)
        if a is not None:
            return a
        ctx.notes.append("no symbol found under the point to use as a template")
        return None
    if not _THIS_ROOM.search(q):
        return None
    room = room_at(pipe, path, ctx)
    if room is None:
        ctx.notes.append("no room found under the point")
        return None
    tag, outline, name = room.get("tag"), room.get("outline"), room.get("name")
    if task == "area" and tag is not None and tag.value:
        sqft = _sqft(q, [tag])
        k = {("ft2", True): 1.0, ("m2", False): 1.0, ("ft2", False): 0.3048 ** 2, ("m2", True): 1 / 0.3048 ** 2}
        v = round(tag.value * k[(tag.unit, sqft)], 2)
        unit = "ft2" if sqft else "m2"
        ev = [_ev(path, ctx, tag.box, f"{tag.label}: {tag.text} (printed)", True)]
        tr = [f"the room under the point: {tag.label}, printed area {tag.text}" + (" (inside the outline traced at the "
                                                                                 "point)" if room.get("in_outline") else
                                                                                 " (the nearest room tag)")]
        if outline is not None and outline.value:
            ev += outline.evidence[:1]
            tr.append(f"cross-check: the outline traced at the point measures {outline.value} {outline.unit}")
        a = Answer(value=v, unit=unit, confidence=0.85 if room.get("in_outline") else 0.7, evidence=ev,
                   solver="pipeline/context", trace=tr, assumptions=[], missing=[])
        return _with_ctx(a, ctx, "point", ctx.line())
    if name:
        q2 = _THIS_ROOM.sub(lambda m: f"the {name}" if m.group(1) else f"in the {name}", q, count=1)
        q2 = re.sub(r"\bthe the\b", "the", q2)
        a = solve(path, q2)
        pick = _at_point(a, pt, ctx.space) if task == "area" and a.value is not None else None
        if pick is not None:
            v, e = pick
            n = sum(1 for x in a.evidence if x.polygon)
            a = a.model_copy(update={
                "value": v, "evidence": [e] + [x for x in a.evidence if x is not e][:5],
                "confidence": round(min(0.8, float(a.confidence or 0) + 0.15), 3),
                "trace": [*a.trace, f"point: the reader measured {n} rooms named '{name}'; the one whose outline "
                                    f"contains your point: {e.note}"]})
            return _with_ctx(a, ctx, "point", f"{ctx.line()}; the room there is '{name}': asked as '{q2}', "
                                              "the outline at the point kept")
        if a.value is not None:
            near = any(e.bbox and e.bbox_space == ctx.space and (
                inside(pt, e.polygon) if e.polygon else _dist_box(pt, e.bbox) < 0.08 * _diag(path)) for e in a.evidence)
            if near or outline is None:
                if not near:
                    a.confidence = round(float(a.confidence or 0) * 0.8, 3)
                return _with_ctx(a, ctx, "point", f"{ctx.line()}; the room there is '{name}': asked as '{q2}'"
                                                  + ("" if near else " (its evidence is not at the point)"))
    if task == "area" and outline is not None and outline.value:
        return _with_ctx(outline, ctx, "point", f"{ctx.line()}; the room outline traced at the point")
    return None


def room_at(pipe, path: str, ctx: Ask) -> dict | None:
    """The room under the point: its outline traced at the point (when a tracer can), the printed tag inside it (else
    the nearest within ~8 % of the sheet diagonal) and its name."""
    from stoa.agent import vision as V

    pt = ctx.point
    g = Answer(value=None, confidence=0.0, solver="context", trace=["the point you clicked vision"])
    key = (str(path), "__context__")
    V._POINTS[key] = (pt[0], pt[1], ctx.space)
    outline = None
    try:
        if ctx.space == "pdf_pt" and _is_pdf(_abs(path)):
            outline = V.measure_at_point(path, "__context__", g)
        if outline is None:
            outline = V.measure_raster_at_point(path, "__context__", g)
    except Exception:  # noqa: BLE001
        outline = None
    finally:
        V._POINTS.pop(key, None)
    if outline is not None:
        outline.trace = [t.replace("the point you clicked located the room", "the room at the point you clicked")
                         for t in outline.trace]
        outline.evidence = [e for e in outline.evidence if e.polygon or e.bbox]
    poly = next((e.polygon for e in (outline.evidence if outline else []) if e.polygon), None)
    tg = tags(path, 0)
    tag, in_outline = None, False
    if poly:
        hits = [t for t in tg if inside(centre(t.box), poly)]
        if len(hits) == 1:
            tag, in_outline = hits[0], True
    if tag is None and tg:
        best = min(tg, key=lambda t: _dist_box(pt, t.box))
        if _dist_box(pt, best.box) < 0.08 * _diag(path):
            tag = best
    name = (tag.name or tag.label) if tag is not None else None
    if not name:
        cands = [(t, bb) for t, bb in lines(path, 0) if _NAMEISH.search(t) and not _NOT_NAME.search(t)
                 and len(t) <= 40 and sum(c.isdigit() for c in t) <= len(t) / 3]
        if poly:
            ins = [c for c in cands if inside(centre(c[1]), poly)]
            if ins:
                name = min(ins, key=lambda c: _dist_box(pt, c[1]))[0]
        if not name and cands:
            best = min(cands, key=lambda c: _dist_box(pt, c[1]))
            if _dist_box(pt, best[1]) < 0.05 * _diag(path):
                name = best[0]
    if tag is None and outline is None and not name:
        return None
    return {"tag": tag, "outline": outline, "name": name, "in_outline": in_outline}


def _what_here(pipe, path, q, ctx) -> Answer | None:
    pt = ctx.point
    tg = tags(path, 0)
    diag = _diag(path)
    hit = [t for t in tg if _dist_box(pt, t.box) <= 0.01 * diag]
    if hit:
        t = hit[0]
        val = f"{t.label}: {t.text}"
        ev = [_ev(path, ctx, t.box, f"{t.label}: {t.text} (printed)", True)]
        a = Answer(value=val, confidence=0.8, evidence=ev, solver="pipeline/context",
                   trace=[f"the room tag under the point: {val}"])
        return _with_ctx(a, ctx, "point", ctx.line())
    ls = [(t, bb) for t, bb in lines(path, 0) if t.strip()]
    near = sorted(ls, key=lambda c: _dist_box(pt, c[1]))[:1]
    if near and _dist_box(pt, near[0][1]) <= 0.01 * diag:
        t, bb = near[0]
        a = Answer(value=t.strip(), confidence=0.75, evidence=[_ev(path, ctx, bb, f"printed: {t.strip()}", True)],
                   solver="pipeline/context", trace=[f"printed at the point: '{t.strip()}'"])
        return _with_ctx(a, ctx, "point", ctx.line())
    room = room_at(pipe, path, ctx)
    if room is None:
        return None
    tag, outline, name = room["tag"], room["outline"], room["name"]
    parts, ev = [], []
    if name:
        parts.append(f"room {name}")
    if tag is not None:
        parts.append(f"printed area {tag.text}")
        ev.append(_ev(path, ctx, tag.box, f"{tag.label}: {tag.text} (printed)", True))
    if outline is not None and outline.value:
        parts.append(f"outline traced at the point ≈ {outline.value} {outline.unit}")
        ev += outline.evidence[:1]
    if not parts:
        return None
    a = Answer(value="; ".join(parts), confidence=0.7 if room["in_outline"] else 0.55, evidence=ev,
               solver="pipeline/context", trace=["at the point: " + "; ".join(parts)],
               assumptions=["the room = the blank region around the point up to the drawn walls"] if outline else [])
    return _with_ctx(a, ctx, "point", ctx.line())


# -- 'count all like this': the clicked symbol as a template (captain 19:47) ------------------------------------------
LIKE_THR = float(os.environ.get("STOA_LIKE_THR", 0.8))
LIKE_THR1 = 0.6  # PDFs: candidates at 150 dpi (a half-pixel phase costs ~0.25), the ~430 dpi re-check decides
HI_DPI = 432


def _thin(g, k: int):
    """Thin-line ink (walls / fills removed by a k x k opening), blurred a little: what a fixture symbol is drawn with.
    Matching on it keeps a wall edge in the template from matching every wall."""
    import cv2
    import numpy as np

    ink = (g < 245).astype(np.uint8)
    thick = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    thin = ink & (1 - cv2.dilate(thick, np.ones((3, 3), np.uint8)))
    return cv2.GaussianBlur(thin.astype(np.float32), (0, 0), 1.0)


def _ncc(img, tpl):
    """TM_CCOEFF_NORMED where the window holds at least a third of the template's ink variance; 0 elsewhere (blank
    paper is exactly 0 on a thin-ink map: its correlation is undefined and came out as matches)."""
    import cv2
    import numpy as np

    res = np.nan_to_num(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
    th, tw = tpl.shape
    n = float(th * tw)
    S = cv2.integral(img.astype(np.float64))
    S2 = cv2.integral((img.astype(np.float64)) ** 2)
    h, w = res.shape

    def win(I):
        return I[th:th + h, tw:tw + w] - I[0:h, tw:tw + w] - I[th:th + h, 0:w] + I[0:h, 0:w]

    var = win(S2) / n - (win(S) / n) ** 2
    res[var < max(1e-6, float(tpl.var()) / 3)] = 0.0
    return res


def _verify_hi(p: str, tbox, centres: list) -> list | None:
    """Best NCC of the template (pdf_pt box) against each candidate centre (pdf_pt), on ~430 dpi renders of small
    clips (a display list: the page is parsed once). None when the PDF cannot be rendered."""
    import cv2
    import numpy as np
    import pymupdf

    Z = HI_DPI / 72
    try:
        with pymupdf.open(p) as doc:
            pg = doc[0]
            if pg.rotation:
                pg.set_rotation(0)
            dl = pg.get_displaylist()
            tw, th = tbox[2] - tbox[0], tbox[3] - tbox[1]

            def clip(x0, y0, x1, y1):
                pix = dl.get_pixmap(matrix=pymupdf.Matrix(Z, Z), clip=pymupdf.Rect(x0, y0, x1, y1),
                                    colorspace=pymupdf.csGRAY, alpha=False)
                return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()

            kh = max(3, int(round(5 * Z / (150 / 72))))  # the wall opening scaled to this resolution
            # the thin map of a PADDED clip, then cropped: a wall cut at the clip's edge is too narrow for the opening
            # to remove (the template then scored 0.64 against itself)
            pad = 2.0 * kh / Z
            big = clip(tbox[0] - pad, tbox[1] - pad, tbox[2] + pad, tbox[3] + pad)
            o = int(round(pad * Z))
            T = _thin(big, kh)[o:big.shape[0] - o, o:big.shape[1] - o]
            if T.size < 64 or float(T.std()) < 0.05:
                return None
            vs, seen = [], set()
            for flip in (False, True):
                t = cv2.flip(T, 1) if flip else T
                for k in range(4):
                    tk = np.ascontiguousarray(np.rot90(t, k))
                    if tk.tobytes() not in seen:
                        seen.add(tk.tobytes())
                        vs.append(tk)
            m = 0.6 * max(tw, th) + 1
            out = []
            for cx, cy in centres[:800]:
                W_ = _thin(clip(cx - m - max(tw, th) / 2, cy - m - max(tw, th) / 2, cx + m + max(tw, th) / 2,
                                cy + m + max(tw, th) / 2), kh)
                best = 0.0
                for tk in vs:
                    if tk.shape[0] <= W_.shape[0] and tk.shape[1] <= W_.shape[1]:
                        best = max(best, float(_ncc(W_, tk).max()))
                out.append(best)
            out += [0.0] * (len(centres) - len(out))
            return out
    except Exception:  # noqa: BLE001
        return None


def _gray(p: str):
    """(grey image, pixels per Evidence unit) of a PDF page (rendered, long side <= 5000 px) or an image."""
    import numpy as np

    if _is_pdf(p):
        import pymupdf

        with pymupdf.open(p) as doc:
            pg = doc[0]
            r = pg.rect
            z = min(150 / 72, 5000 / max(r.width, r.height))
            # unrotated page space (Evidence space): render with the page's rotation undone
            rot = pg.rotation
            if rot:
                pg.set_rotation(0)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
        g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return g, z
    import cv2

    g = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if g is None:
        return None, 1.0
    f = min(1.0, 6000 / max(g.shape))
    if f < 1.0:
        g = cv2.resize(g, (int(g.shape[1] * f), int(g.shape[0] * f)), interpolation=cv2.INTER_AREA)
    return g, f


def _template_box(ink, x: int, y: int, r: int, max_side: int):
    """The ink component under (or nearest to) the point; a window around the point when it is part of a large
    figure (a door swing joined to its wall)."""
    import cv2
    import numpy as np

    H, W = ink.shape
    x0, y0, x1, y1 = max(0, x - r), max(0, y - r), min(W, x + r + 1), min(H, y + r + 1)
    win = ink[y0:y1, x0:x1]
    if not win.any():
        return None, "no ink near the point"
    ys, xs = np.nonzero(win)
    k = int(np.argmin((xs + x0 - x) ** 2 + (ys + y0 - y) ** 2))
    sx, sy = int(xs[k] + x0), int(ys[k] + y0)
    big = max_side * 3
    bx0, by0, bx1, by1 = max(0, sx - big), max(0, sy - big), min(W, sx + big + 1), min(H, sy + big + 1)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink[by0:by1, bx0:bx1].astype(np.uint8), connectivity=8)
    li = lab[sy - by0, sx - bx0]
    cx, cy, cw, ch, area = (int(v) for v in st[li])
    if 4 <= max(cw, ch) <= max_side and area >= 8 and not (cx == 0 or cy == 0 or cx + cw >= bx1 - bx0 or
                                                           cy + ch >= by1 - by0):
        return (cx + bx0 - 2, cy + by0 - 2, cx + bx0 + cw + 2, cy + by0 + ch + 2), "the symbol (ink component) at the point"
    h = max(8, max_side // 3)
    return (x - h, y - h, x + h, y + h), "a window around the point (the symbol is joined to other lines)"


def count_like(pipe, path: str, q: str, ctx: Ask) -> Answer | None:
    """Count every copy of the clicked symbol: normalised cross-correlation of its picture at 8 orientations (rotations
    x mirror), peaks >= LIKE_THR, one per spot; limited to the view the point lies in when the sheet has views."""
    import cv2
    import numpy as np

    p = _abs(path)
    g, z = _gray(p)
    if g is None:
        return None
    x, y = int(round(ctx.point[0] * z)), int(round(ctx.point[1] * z))
    H, W = g.shape
    if not (0 <= x < W and 0 <= y < H):
        return None
    long_px = max(H, W)
    # fixtures are drawn in thin (often light grey) lines and touch the walls: every non-white pixel is ink, and the
    # thick / filled walls are taken out (morphological opening) so the WC bowl is its own component (DfE sheet)
    ink = g < 245  # anti-aliased light-grey lines at 150 dpi are 200-240 (DfE WC bowl)
    k = max(3, long_px // 700)
    thick = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    thin = ink & ~cv2.dilate(thick, np.ones((3, 3), np.uint8)).astype(bool)
    box, how = _template_box(thin, x, y, r=max(8, long_px // 150), max_side=max(16, long_px // 40))
    if box is None:
        return None
    bx0, by0, bx1, by1 = (int(v) for v in (max(0, box[0]), max(0, box[1]), min(W, box[2]), min(H, box[3])))
    gt = _thin(g, k)
    tpl = gt[by0:by1, bx0:bx1]
    if tpl.size < 64 or float(tpl.std()) < 0.05:
        return None
    # the view the point lies in (VP): matches outside it are another drawing (an enlarged plan, the legend)
    zone, vname = None, None
    try:
        from stoa.sheet import views

        lay = _layer(p)
        for v in views(p, 0, layer=lay) if (_is_pdf(p) or lay is not None) else []:
            if v.bbox and v.contains(ctx.point[0], ctx.point[1], pad=2):
                zone, vname = [c * z for c in v.bbox], v.title
                break
    except Exception:  # noqa: BLE001
        zone = None
    if zone:
        X0, Y0 = max(0, int(zone[0]) - tpl.shape[1]), max(0, int(zone[1]) - tpl.shape[0])
        X1, Y1 = min(W, int(zone[2]) + tpl.shape[1]), min(H, int(zone[3]) + tpl.shape[0])
    else:
        X0, Y0, X1, Y1 = 0, 0, W, H
    area = gt[Y0:Y1, X0:X1]
    cv2.setNumThreads(4)
    variants, seen = [], set()
    for flip in (False, True):
        t = cv2.flip(tpl, 1) if flip else tpl
        for k in range(4):
            tk = np.ascontiguousarray(np.rot90(t, k))
            key = tk.tobytes()
            if key in seen:
                continue
            seen.add(key)
            variants.append(tk)
    cands = []
    for tk in variants:
        if tk.shape[0] > area.shape[0] or tk.shape[1] > area.shape[1]:
            continue
        res = _ncc(area, tk)
        ys, xs = np.nonzero(res >= (LIKE_THR1 if _is_pdf(p) else LIKE_THR))
        if len(xs) > 40000:
            return None  # the 'symbol' is plain hatching / a line: everything matches
        for yy, xx in zip(ys, xs):
            cands.append((float(res[yy, xx]), int(xx) + X0, int(yy) + Y0, tk.shape[1], tk.shape[0]))
    cands.sort(reverse=True)
    peaks = []
    rad = 0.5 * min(tpl.shape)
    for sc, xx, yy, tw, th in cands:
        cx, cy = xx + tw / 2, yy + th / 2
        if all(math.hypot(cx - px, cy - py) > rad for _s, px, py, _w, _h in peaks):
            peaks.append((sc, cx, cy, tw, th))
    if not peaks:
        return None
    k = 1 / z
    stage = f"normalised cross-correlation >= {LIKE_THR:.2f}"
    if _is_pdf(p):  # 150 dpi cannot tell a WC bowl from a chair (DfE hall seats): re-check each candidate at ~430 dpi
        hi = _verify_hi(p, (bx0 * k, by0 * k, bx1 * k, by1 * k), [(cx * k, cy * k) for _s, cx, cy, _w, _h in peaks])
        if hi is not None:
            n0 = len(peaks)
            peaks = [(sh, cx, cy, tw, th) for (_s, cx, cy, tw, th), sh in zip(peaks, hi) if sh >= LIKE_THR]
            stage = (f"{n0} candidates at 150 dpi (>= {LIKE_THR1:.2f}), each re-checked at {HI_DPI} dpi: "
                     f">= {LIKE_THR:.2f} kept")
            if not peaks:
                return None
    ev = [_ev(path, ctx, [(cx - tw / 2) * k, (cy - th / 2) * k, (cx + tw / 2) * k, (cy + th / 2) * k],
              f"like the clicked symbol ({sc:.2f})", False) for sc, cx, cy, tw, th in peaks[:400]]
    n = len(peaks)
    conf = 0.55 if n >= 2 else 0.35
    a = Answer(value=n, unit="count", confidence=conf, evidence=ev, solver="pipeline/context",
               trace=[f"template = {how}, {tpl.shape[1]}x{tpl.shape[0]} px; matched at {len(variants)} orientations "
                      f"({stage}) " + (f"inside view '{vname}'" if vname else
                                                                               "over the whole sheet")
                      + f": {n} copies (the clicked one included)"],
               assumptions=["'like this' = drawn the same way as the clicked symbol (same size, any rotation or "
                            "mirror); differently drawn variants are not counted"]
               + ([f"counted inside the view '{vname}' only"] if vname else []),
               missing=["symbols that touch other lines or text may be missed: check the marks"])
    return _with_ctx(a, ctx, "point", ctx.line())
