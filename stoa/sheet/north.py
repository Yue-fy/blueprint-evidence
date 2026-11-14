"""North arrow of a sheet (VP, captain 20:47: facade questions such as 'windows on the west facade' need to know which
side of the drawing faces west).

    north_arrow("sheet.pdf")   # {'found': True, 'angle_deg': 0.0, 'sides': {'north': 'top', 'west': 'left', ...}, ...}

angle_deg = the direction north points on the DISPLAYED sheet, degrees clockwise from the sheet's up (0 = up, 90 =
right). Vector PDFs: an 'N' / 'NORTH' / 'PLAN NORTH' / 'TRUE NORTH' label next to a small drawn symbol (circle, needle,
arrow): north = from the symbol's centre to the label when the label sits outside the symbol, else the label's own
rotation, else the symbol's filled arrow tip. A letter of a letter-spaced word ('N I A ' S') or an 'N' with no small
symbol around it is not an arrow. Not found -> found False, angle 0 with assumed True: the top of the sheet is taken as
north (the expert's convention on Stoa's DfE sheets) and the note says so. Images: not read yet (found False).
Deterministic, ~0.1-0.5 s per page, $0.
"""

from __future__ import annotations

import math
import re

_LABEL = re.compile(r"^(?:N|N\.|NORTH|PLAN\s+NORTH|TRUE\s+NORTH|PROJECT\s+NORTH|NORD|NORR|POHJOINEN)$", re.I)
_SIDES = ["top", "right", "bottom", "left"]


def _sides(angle: float) -> dict:
    """Compass direction -> side of the displayed sheet it faces (nearest of top / right / bottom / left)."""
    out = {}
    for k, off in (("north", 0), ("east", 90), ("south", 180), ("west", 270)):
        out[k] = _SIDES[int(((angle + off) % 360 + 45) // 90) % 4]
    return out


def _not_found(note: str) -> dict:
    return {"found": False, "angle_deg": 0.0, "assumed": True, "sides": _sides(0.0), "method": None, "bbox": None,
            "confidence": 0.3, "note": note}


def _tip(near) -> tuple | None:
    """centroid -> farthest vertex of the symbol's dark filled arrow head (a dart / half arrow), or None."""
    import pymupdf

    fills = [dr for dr in near if dr.get("fill") is not None and sum(dr["fill"]) < 1.5 and dr.get("items")
             and sum(it[0] == "l" for it in dr["items"]) >= 2]
    pts = [p for dr in fills for it in dr["items"] for p in it[1:] if isinstance(p, pymupdf.Point)]
    if len(pts) < 3:
        return None
    fx, fy = sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts)
    t = max(pts, key=lambda p: (p.x - fx) ** 2 + (p.y - fy) ** 2)
    return (t.x - fx, t.y - fy) if (t.x, t.y) != (fx, fy) else None


def _cos(a, b) -> float:
    na, nb = math.hypot(*a), math.hypot(*b)
    return (a[0] * b[0] + a[1] * b[1]) / (na * nb) if na and nb else 0.0


def _disp(pg, d) -> tuple:
    """A direction vector from unrotated page space to the displayed sheet."""
    import pymupdf

    m = pg.rotation_matrix
    o, p = pymupdf.Point(0, 0) * m, pymupdf.Point(*d) * m
    return (p.x - o.x, p.y - o.y)


def north_arrow(path: str, page: int = 0, layer=None) -> dict:
    """See the module doc. bbox = the arrow symbol + label in pdf_pt (unrotated page space, CONTRACT evidence)."""
    if not str(path).lower().endswith(".pdf"):
        return _image_arrow(str(path), layer)
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception:  # noqa: BLE001
        return _not_found("could not open the PDF: the top of the sheet is taken as north")
    with doc:
        if page >= len(doc):
            return _not_found("no such page")
        pg = doc[page]
        lines = []
        for b in pg.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                t = " ".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if t:
                    size = max((s.get("size", 0) for s in ln.get("spans", [])), default=0) or 8.0
                    lines.append((t, pymupdf.Rect(ln["bbox"]), tuple(ln.get("dir", (1, 0))), size))
        cands = [x for x in lines if _LABEL.match(x[0])]
        if not cands:
            return _not_found("no north arrow found on the sheet: the top of the sheet is taken as north (check)")
        try:
            drawings = pg.get_drawings()
        except Exception:  # noqa: BLE001
            drawings = []
        best = None
        for t, r, d, z in cands:
            c = (r.tl + r.br) / 2
            # a letter of a letter-spaced word: other text right next to it on the same baseline
            if len(t) <= 2 and any(o is not None and o[1] != r and abs(((o[1].y0 + o[1].y1) / 2) - c.y) < 0.4 * z
                                   and min(abs(o[1].x0 - r.x1), abs(r.x0 - o[1].x1)) < 1.2 * z
                                   for o in lines):
                continue
            # a legend column ('(S) TEMPERATURE', '(N) NIGHT …', '(PB) …'): other short labels of the same size
            # stacked right above / below the label
            col = [o for o in lines if o[1] != r and len(o[0]) <= 3 and abs(o[3] - z) <= 0.25 * z
                   and abs((o[1].x0 + o[1].x1) / 2 - c.x) <= 2 * z and 0 < abs((o[1].y0 + o[1].y1) / 2 - c.y) <= 12 * z]
            if len(col) >= 2:
                continue
            # a word list (an abbreviations column: 'MULLION' / 'NORTH' / 'NATURAL'): left-aligned lines just above + below
            lst = [o for o in lines if o[1] != r and abs(o[1].x0 - r.x0) <= 1.0 and abs(o[3] - z) <= 0.15 * z
                   and 0 < abs(o[1].y0 - r.y0) <= 3.5 * z]
            if len(lst) >= 2:
                continue
            small = [dr for dr in drawings if dr.get("rect") is not None and 0 < max(dr["rect"].width, dr["rect"].height)
                     <= min(14 * z, 150.0)                 # plan geometry / frames are not a symbol
                     and not (dr.get("color") is None and dr.get("fill") is not None and sum(dr["fill"]) > 2.7)]  # text masks
            # the symbol = small drawings touching the label's surroundings (2 z), then whatever touches those
            zone = pymupdf.Rect(r.x0 - 2 * z, r.y0 - 2 * z, r.x1 + 2 * z, r.y1 + 2 * z)
            near = [dr for dr in small if dr["rect"].intersects(zone)]
            if near:
                u = pymupdf.Rect(near[0]["rect"])
                for dr in near[1:]:
                    u |= dr["rect"]
                near += [dr for dr in small if dr not in near and dr["rect"].intersects(u)
                         and max(dr["rect"].width, dr["rect"].height) <= 1.5 * max(u.width, u.height)]
            if not near:
                continue
            sym = pymupdf.Rect(near[0]["rect"])
            for dr in near[1:]:
                sym |= dr["rect"]
            sc = (sym.tl + sym.br) / 2
            v = (c.x - sc.x, c.y - sc.y)
            method = None
            tip = _tip(near)
            if not sym.contains(c) and math.hypot(*v) > 0.6 * z:
                method = "label outside the symbol"
                if tip is not None and _cos(tip, v) > 0.5:  # the arrow head agrees with the label's side: it is exact
                    v, method = tip, "arrow tip (label agrees)"
            elif abs(_disp(pg, d)[1]) > 0.05:             # a letter rotated on the DISPLAYED sheet: its own up
                v, method = (d[1], -d[0]), "rotated label"
            else:                                         # a dark filled arrow head (not a white disc)
                fills = [dr for dr in near if dr.get("fill") is not None and sum(dr["fill"]) < 1.5 and dr.get("items")
                         and sum(it[0] == "l" for it in dr["items"]) >= 2]
                pts = [p for dr in fills for it in dr["items"] for p in it[1:] if isinstance(p, pymupdf.Point)]
                if len(pts) >= 3:
                    fc = pymupdf.Point(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))
                    tip = max(pts, key=lambda p: (p.x - fc.x) ** 2 + (p.y - fc.y) ** 2)
                    v, method = (tip.x - fc.x, tip.y - fc.y), "filled arrow tip"
            if method is None or math.hypot(*v) == 0:
                continue
            score = (2 if len(t) > 2 else 1) + (1 if method != "filled arrow tip" else 0.5) + min(len(near), 6) / 6
            if best is None or score > best[0]:
                best = (score, t, r | sym, v, method)
        if best is None:
            return _not_found("an 'N' label but no north-arrow symbol next to it: the top of the sheet is taken as "
                              "north (check)")
        _s, t, box, v, method = best
        m = pg.rotation_matrix
        o = pymupdf.Point(0, 0) * m
        p = pymupdf.Point(*v) * m
        dx, dy = p.x - o.x, p.y - o.y                    # displayed page, y down
        ang = math.degrees(math.atan2(dx, -dy)) % 360
        snap = min((0, 90, 180, 270, 360), key=lambda a: abs(a - ang))
        if abs(snap - ang) <= 3:
            ang = float(snap % 360)
        return {"found": True, "angle_deg": round(ang, 1), "assumed": False, "sides": _sides(ang), "method": method,
                "bbox": [round(x, 1) for x in box], "label": t, "confidence": 0.8 if method != "filled arrow tip" else 0.65,
                "note": f"north arrow '{t}' ({method}): north points {round(ang)} deg clockwise from the sheet's top"}


def _image_arrow(path: str, layer=None) -> dict:
    """Images: the label from the image's text layer (the caller's, else an already cached one; never a cold OCR),
    the symbol = dark connected components touching the label's surroundings; same rules as on PDFs."""
    import os

    import numpy as np
    from PIL import Image

    if layer is None:
        from stoa.sheet.sets import _cached_layer

        layer = _cached_layer(os.path.abspath(path))
    if layer is None or not getattr(layer, "items", None):
        return _not_found("image text not read yet: the top of the sheet is taken as north (check)")
    items = [(it.text.strip(), tuple(it.box)) for it in layer.items if it.text.strip()]
    cands = [x for x in items if _LABEL.match(x[0])]
    if not cands:
        return _not_found("no north arrow found on the sheet: the top of the sheet is taken as north (check)")
    try:
        import cv2

        im = Image.open(path).convert("L")
    except Exception:  # noqa: BLE001
        return _not_found("could not read the image: the top of the sheet is taken as north")
    W, H = im.size
    best = None
    for t, b in cands:
        h = max(4.0, b[3] - b[1])
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        col = [o for o in items if o[1] != b and len(o[0]) <= 3 and abs((o[1][3] - o[1][1]) - h) <= 0.3 * h
               and abs((o[1][0] + o[1][2]) / 2 - cx) <= 2 * h and 0 < abs((o[1][1] + o[1][3]) / 2 - cy) <= 12 * h]
        lst = [o for o in items if o[1] != b and abs(o[1][0] - b[0]) <= 0.3 * h and abs((o[1][3] - o[1][1]) - h) <= 0.25 * h
               and 0 < abs(o[1][1] - b[1]) <= 3.5 * h]
        side = [o for o in items if o[1] != b and len(t) <= 2 and abs((o[1][1] + o[1][3]) / 2 - cy) < 0.4 * h
                and min(abs(o[1][0] - b[2]), abs(b[0] - o[1][2])) < 1.2 * h]
        if len(col) >= 2 or len(lst) >= 2 or side:
            continue
        R = int(10 * h)
        x0, y0, x1, y1 = max(0, int(cx - R)), max(0, int(cy - R)), min(W, int(cx + R)), min(H, int(cy + R))
        a = np.asarray(im.crop((x0, y0, x1, y1))) < 128
        lb = (int(b[0]) - x0 - 2, int(b[1]) - y0 - 2, int(b[2]) - x0 + 2, int(b[3]) - y0 + 2)
        a[max(0, lb[1]):max(0, lb[3]), max(0, lb[0]):max(0, lb[2])] = False      # the label's own ink
        for _o, ob in items:                       # and every other text's ink ('PLAN' above 'NORTH'): not a symbol
            if ob[2] > x0 and ob[0] < x1 and ob[3] > y0 and ob[1] < y1:
                a[max(0, int(ob[1]) - y0 - 1):max(0, int(ob[3]) - y0 + 1), max(0, int(ob[0]) - x0 - 1):max(0, int(ob[2]) - x0 + 1)] = False
        n, lab, stats, _c = cv2.connectedComponentsWithStats(a.astype(np.uint8), connectivity=8)
        if n <= 1:
            continue
        zone = (lb[0] - 2 * h, lb[1] - 2 * h, lb[2] + 2 * h, lb[3] + 2 * h)
        near = []
        for i in range(1, n):
            sx, sy, sw, sh = (int(x) for x in stats[i][:4])
            bb = (sx, sy, sx + sw, sy + sh)
            if max(bb[2] - bb[0], bb[3] - bb[1]) > 14 * h or max(bb[2] - bb[0], bb[3] - bb[1]) < 0.3 * h:
                continue
            if bb[0] <= zone[2] and bb[2] >= zone[0] and bb[1] <= zone[3] and bb[3] >= zone[1]:
                near.append((i, bb))
        if not near:
            continue
        ux0, uy0 = min(q[1][0] for q in near), min(q[1][1] for q in near)
        ux1, uy1 = max(q[1][2] for q in near), max(q[1][3] for q in near)
        sc = ((ux0 + ux1) / 2, (uy0 + uy1) / 2)
        lc = (cx - x0, cy - y0)
        v = (lc[0] - sc[0], lc[1] - sc[1])
        inside = ux0 <= lc[0] <= ux1 and uy0 <= lc[1] <= uy1
        if inside or math.hypot(*v) <= 0.6 * h:
            continue                                   # label inside its symbol: no reliable cue on a raster yet
        method = "label outside the symbol"
        # a dark filled arrow head: the densest component; its tip = the pixel farthest from its centroid
        dens = []
        for k, bb in near:
            m = lab[bb[1]:bb[3], bb[0]:bb[2]] == k
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            if area and m.sum() / area > 0.3 and m.sum() > 20:
                dens.append((m.sum(), k, bb, m))
        if dens:
            _a, k, bb, m = max(dens)
            yy, xx = np.nonzero(m)
            fx, fy = xx.mean(), yy.mean()
            j = int(np.argmax((xx - fx) ** 2 + (yy - fy) ** 2))
            tip = (xx[j] - fx, yy[j] - fy)
            if _cos(tip, v) > 0.5:
                v, method = tip, "arrow tip (label agrees)"
        score = (2 if len(t) > 2 else 1) + min(len(near), 6) / 6
        if best is None or score > best[0]:
            best = (score, t, (x0 + ux0, y0 + uy0, x0 + ux1, y0 + uy1), v, method)
    if best is None:
        return _not_found("an 'N' label but no north-arrow symbol next to it: the top of the sheet is taken as north "
                          "(check)")
    _s, t, box, v, method = best
    ang = math.degrees(math.atan2(v[0], -v[1])) % 360
    snap = min((0, 90, 180, 270, 360), key=lambda a: abs(a - ang))
    if abs(snap - ang) <= 3:
        ang = float(snap % 360)
    return {"found": True, "angle_deg": round(ang, 1), "assumed": False, "sides": _sides(ang), "method": method,
            "bbox": [round(float(x), 1) for x in box], "bbox_space": "image_px", "label": t, "confidence": 0.6,
            "note": f"north arrow '{t}' ({method}, read on the image): north points {round(ang)} deg clockwise from the "
                    f"sheet's top"}
