"""KEY PLAN zones (VP c6, captain 23:04: 'How many doors are in the north wing?' on AFCEC A-2 needs the REGION first).

A key plan is the small diagram of the whole building in a corner of the sheet; the part this sheet shows (or each wing /
module / phase) is a coloured or hatched block. Wing names are often not printed in it: AFCEC A-2 names 'WEST / EAST /
NORTH WING' only in its area table, and the key plan's north arrow ('PLAN NORTH', up) tells which block is which.

    from stoa.sheet.keyplan import key_plan_zones, zone_for
    kp = key_plan_zones("A-2.pdf")       # {found, zones: [{name, side, key_box, plan_box, colour, share}], key_box,
                                         #  plan_box, north, note, confidence}; boxes in page space (pdf_pt unrotated /
                                         #  image px), like every evidence box
    z = zone_for("A-2.pdf", "How many doors are in the north wing?")   # the zone dict (+ 'why'), or None

Method (no model, ~0.2 s): the 'KEY PLAN' title (stoa.sheet.views); the coloured blocks in a window around it, rendered
(vector fills are often clipped rectangles, so pixels, not paths); each colour's connected blocks = zones (slivers merge
into the zone they touch); the block nearest the key plan's centre is the core, the others are named by their compass side
(north from stoa.sheet.north_arrow; none found -> top = north, said so); the key plan's extent is fitted onto the main
plan's building extent (the largest plan view's inked blob) with independent x / y scales, so each zone gets a box on the
main plan. Images need their text layer (already read; never OCRs here).
"""
from __future__ import annotations

import math
import re

import numpy as np

_SIDES = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")
_Q_SIDE = re.compile(r"\b(north|south|east|west|northern|southern|eastern|western|north[\s-]?east|north[\s-]?west|"
                     r"south[\s-]?east|south[\s-]?west|centre|center|central|middle|core)\b(?:[\s-]+(?:most\s+)?)?"
                     r"(wing|block|module|part|zone|section|side|end|arm|leg|area|portion|half)?", re.I)


def _norm_side(w: str) -> str:
    w = w.lower().replace(" ", "-")
    w = {"northern": "north", "southern": "south", "eastern": "east", "western": "west", "center": "core",
         "centre": "core", "central": "core", "middle": "core"}.get(w, w)
    if re.fullmatch(r"(north|south)-?(east|west)", w):
        w = re.sub(r"(north|south)-?(east|west)", r"\1-\2", w)
    return w


def _render(path: str, page: int, box, dpi_px: int = 500):
    """(RGB array, px per page unit, origin) of a DISPLAYED-page box (pdf: displayed pt; image: px)."""
    if str(path).lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(path) as doc:
            pg = doc[int(page)]
            r = pymupdf.Rect(*box) & pg.rect
            z = dpi_px / max(1.0, max(r.width, r.height))
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=r, alpha=False)
            a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
            return a, z, (r.x0, r.y0)
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(path) as im:
        x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
        x1, y1 = min(im.width, int(box[2])), min(im.height, int(box[3]))
        z = dpi_px / max(1, max(x1 - x0, y1 - y0))
        crop = im.convert("RGB").crop((x0, y0, x1, y1))
        crop = crop.resize((max(1, int((x1 - x0) * z)), max(1, int((y1 - y0) * z))))
        return np.asarray(crop).copy(), z, (x0, y0)


def _to_page(path: str, page: int, box):
    """Displayed box -> page space (pdf_pt unrotated; images unchanged)."""
    if not str(path).lower().endswith(".pdf"):
        return [round(float(v), 1) for v in box]
    import pymupdf

    with pymupdf.open(path) as doc:
        pg = doc[int(page)]
        r = pymupdf.Rect(*box) * pg.derotation_matrix if pg.rotation else pymupdf.Rect(*box)
    r.normalize()
    return [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)]


def _to_disp(path: str, page: int, box):
    if not box:
        return None
    if not str(path).lower().endswith(".pdf"):
        return tuple(box)
    import pymupdf

    with pymupdf.open(path) as doc:
        pg = doc[int(page)]
        r = pymupdf.Rect(*box) * pg.rotation_matrix if pg.rotation else pymupdf.Rect(*box)
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _blocks(img: np.ndarray):
    """Coloured blocks of a small render: [(mask, colour)] — fills only (pale or saturated colour), lines / text /
    white left out (slivers are merged later, within the key plan: _merge_slivers)."""
    import cv2

    mx, mn = img.max(axis=2).astype(int), img.min(axis=2).astype(int)
    col = ((mx - mn) >= 10) & (mn < 248)
    if col.sum() < 50:
        return []
    q = (img // 24).astype(np.int32)
    key = (q[:, :, 0] * 64 + q[:, :, 1] * 8 + q[:, :, 2] + 1) * col   # +1: a very dark colour is not key 0 (= no colour)
    out = []
    for k in np.unique(key[col]):
        m = (key == k).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, lab, st, _c = cv2.connectedComponentsWithStats(m, connectivity=4)
        for i in range(1, n):
            if st[i, cv2.CC_STAT_AREA] >= 20:
                mm = lab == i
                out.append([mm, tuple(int(v) for v in np.median(img[mm], axis=0))])
    return out


def _merge_slivers(blocks: list, min_share: float = 0.05) -> list:
    """Blocks under min_share of the key plan's coloured area merge into the block they touch most (else drop)."""
    import cv2

    if not blocks:
        return []
    total = sum(int(b[0].sum()) for b in blocks)
    big = [[b[0], b[1]] for b in blocks if b[0].sum() >= min_share * total]
    small = [b for b in blocks if b[0].sum() < min_share * total]
    for s_ in small:
        grown = cv2.dilate(s_[0].astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        touch = [(int((grown & b[0]).sum()), j) for j, b in enumerate(big)]
        t, j = max(touch, default=(0, -1))
        if t > 0:
            big[j][0] = big[j][0] | s_[0]
    return big


def _grey_blocks(img: np.ndarray, min_share: float = 0.05):
    """B/W scans / prints: the key plan's blocks are grey tones (scanned AFCEC A-2: paper 231, wings ~209, core ~197).
    Blur, keep the grey band 12-45 levels below the paper, drop thin strokes, then split a compact darker part off each
    block (the core inside the bar). Colours = the tone groups ('#c5c5c5'); tones within 6 levels count as one."""
    import cv2

    g = img.min(axis=2).astype(np.uint8)
    b = cv2.blur(g, (5, 5))
    paper = float(np.percentile(b, 90))
    band = ((b >= paper - 45) & (b <= paper - 12)).astype(np.uint8)
    band = cv2.morphologyEx(band, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    n, lab, st, _c = cv2.connectedComponentsWithStats(band, connectivity=4)
    parts = []
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] < 150:
            continue
        m = lab == i
        med = float(np.median(b[m]))
        dark = (m & (b < med - 6)).astype(np.uint8)
        dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        nd, labd, std, _cd = cv2.connectedComponentsWithStats(dark, connectivity=4)
        cut = np.zeros_like(m)
        for j in range(1, nd):
            x, y, w, h, a = (int(v) for v in std[j])
            if a >= 0.08 * m.sum() and a >= 0.6 * w * h:          # a compact darker rectangle: its own block
                cut |= labd == j
        if cut.any():
            parts.append(cut)
            rest = (m & ~cv2.dilate(cut.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)).astype(np.uint8)
            nr, labr, str_, _cr = cv2.connectedComponentsWithStats(rest, connectivity=4)
            parts += [labr == k for k in range(1, nr) if str_[k, cv2.CC_STAT_AREA] >= 150]
        else:
            parts.append(m)
    if not parts:
        return []
    total = sum(int(p.sum()) for p in parts)
    parts = [p for p in parts if p.sum() >= min_share * total]
    tones = [float(np.median(b[p])) for p in parts]
    groups: list[list[int]] = []
    for k in sorted(range(len(parts)), key=lambda k: tones[k]):
        if groups and tones[k] - tones[groups[-1][-1]] <= 6:
            groups[-1].append(k)
        else:
            groups.append([k])
    out = []
    for grp in groups:
        t = int(round(sum(tones[k] for k in grp) / len(grp)))
        out += [[parts[k], (t, t, t)] for k in grp]
    return out


def _bearing(dx: float, dy: float, north_deg: float) -> str:
    """Compass side of a displayed-page vector (y down) when north points north_deg clockwise from the sheet's top."""
    b = (math.degrees(math.atan2(dx, -dy)) - north_deg) % 360.0
    return _SIDES[int(((b + 22.5) % 360) // 45)]


def _plan_extent(path: str, page: int, view_disp, coloured: bool = True):
    """The main plan's building extent (displayed units). A colour-filled plan (its key plan is coloured too): the
    extent of its fills (the dimension strings around it are black); else the largest inked blob."""
    import cv2

    img, z, (ox, oy) = _render(path, page, view_disp, 900)
    col = ((img.max(axis=2).astype(int) - img.min(axis=2)) >= 10) & (img.min(axis=2) < 248)
    if coloured and col.sum() >= 0.02 * col.size:
        k = max(3, int(0.006 * max(col.shape)))
        m = cv2.morphologyEx(col.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        n, lab, st, _c = cv2.connectedComponentsWithStats(m, connectivity=8)
        if n > 1:
            big = st[1:, cv2.CC_STAT_AREA].max()
            keep = [i for i in range(1, n) if st[i, cv2.CC_STAT_AREA] >= 0.02 * big]
            x0 = min(st[i, 0] for i in keep)
            y0 = min(st[i, 1] for i in keep)
            x1 = max(st[i, 0] + st[i, 2] for i in keep)
            y1 = max(st[i, 1] + st[i, 3] for i in keep)
            return (ox + x0 / z, oy + y0 / z, ox + x1 / z, oy + y1 / z)
    ink = (img.min(axis=2) < 200) | col
    k = max(3, int(0.012 * max(ink.shape)))
    m = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    n, lab, st, _c = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    x, y, w, h = (int(v) for v in st[i, :4])
    return (ox + x / z, oy + y / z, ox + (x + w) / z, oy + (y + h) / z)


def _not_found(note: str) -> dict:
    return {"found": False, "zones": [], "key_box": None, "plan_box": None, "north": None, "note": note,
            "confidence": 0.0}


def key_plan_zones(path: str, page: int = 0, layer=None) -> dict:
    """See the module doc. Never raises."""
    try:
        return _key_plan_zones(str(path), int(page), layer)
    except Exception as e:  # noqa: BLE001
        return _not_found(f"key plan not read ({type(e).__name__}: {e})")


def _key_plan_zones(path: str, page: int, layer) -> dict:
    import cv2

    from stoa.sheet.north import north_arrow
    from stoa.sheet.views import PLAN_KINDS, views

    is_pdf = path.lower().endswith(".pdf")
    if not is_pdf and layer is None:
        try:
            from stoa.sheet.sets import _cached_layer

            layer = _cached_layer(path)
        except Exception:  # noqa: BLE001
            layer = None
        if layer is None:
            return _not_found("no text layer read yet for this picture: the key plan's title cannot be found")
    vs = views(path, page, layer=layer) if not is_pdf else views(path, page)
    kps = [v for v in vs if v.kind == "key_plan"]
    if not kps:
        return _not_found("no KEY PLAN titled on this sheet")
    kp = kps[0]
    t = _to_disp(path, page, kp.title_bbox)
    tw, th = t[2] - t[0], t[3] - t[1]
    W = max(tw, th)                    # the title's length (a vertical title on a sideways sheet is tall)
    win = (t[0] - 2.5 * W, t[1] - 3.5 * W, t[2] + 2.5 * W, t[3] + 3.5 * W)
    img, z, (ox, oy) = _render(path, page, win, 600)
    bl = _blocks(img)
    grey = False
    if not bl:
        bl, grey = _grey_blocks(img), True
    if not bl:
        return _not_found("no coloured or grey blocks around the KEY PLAN title (hatched / outline-only key plans are "
                          "not read)")
    # the key plan = the blocks nearest the title: the cluster (blocks within ~0.5 title lengths of each other)
    # holding the block closest to the title centre
    tcx, tcy = ((t[0] + t[2]) / 2 - ox) * z, ((t[1] + t[3]) / 2 - oy) * z
    boxes = []
    for m, c in bl:
        ys, xs = np.nonzero(m)
        boxes.append((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    hh, ww = img.shape[:2]
    # a block running off the window belongs to something larger (the main plan's fills beside a key plan: DAF dorm
    # A-4's green module wing next to its KEY PLAN title)
    inside = [i for i, b in enumerate(boxes) if b[0] > 2 and b[1] > 2 and b[2] < ww - 2 and b[3] < hh - 2]
    if not inside:
        return _not_found("no key plan drawing found inside the window around its title")

    def gap(a, b):
        return max(0, max(a[0], b[0]) - min(a[2], b[2]), max(a[1], b[1]) - min(a[3], b[3]))

    def dist_t(b):   # a key plan sits centred under / over its title: sideways offsets count double
        return math.hypot(2.0 * max(0, b[0] - tcx, tcx - b[2]), max(0, b[1] - tcy, tcy - b[3]))

    start = min(inside, key=lambda i: dist_t(boxes[i]))
    if dist_t(boxes[start]) > 2.5 * W * z:
        return _not_found("no coloured key plan next to its title")
    group, todo = {start}, [start]
    while todo:
        i = todo.pop()
        for j in inside:
            if j not in group and gap(boxes[i], boxes[j]) <= 0.5 * W * z:
                group.add(j)
                todo.append(j)
    zs = _merge_slivers([bl[i] for i in sorted(group)])
    allm = np.zeros(img.shape[:2], bool)
    for m, _c in zs:
        allm |= m
    ys, xs = np.nonzero(allm)
    kx0, ky0, kx1, ky1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    kcx, kcy = (kx0 + kx1) / 2, (ky0 + ky1) / 2
    na = north_arrow(path, page, layer=layer) if not is_pdf else north_arrow(path, page)
    north_deg = float(na.get("angle_deg") or 0.0) if na.get("found") else 0.0
    north_note = (f"north from the sheet's north arrow ({na.get('label') or 'arrow'})" if na.get("found") else
                  "no north arrow found: the top of the sheet is taken as north")
    zones = []
    tot = float(allm.sum())
    for m, c in zs:
        yy, xx = np.nonzero(m)
        cx, cy = float(xx.mean()), float(yy.mean())
        zones.append({"mask": m, "colour": "#%02x%02x%02x" % c, "share": round(float(m.sum()) / tot, 3),
                      "box_px": (int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1),
                      "off": ((cx - kcx) / max(1, kx1 - kx0), (cy - kcy) / max(1, ky1 - ky0))})
    # the core / hub: a block whose colour no other block shares while the wings share one (AFCEC: pink common
    # area between green wings), else the block nearest the key plan's centre; sides are read FROM the core (a T's
    # bbox centre lies in its stem)
    cols = [q["colour"] for q in zones]
    uniq = [q for q in zones if cols.count(q["colour"]) == 1 and q["share"] >= 0.08]
    core = None
    if len(zones) >= 3 and uniq and len(uniq) < len(zones):
        core = min(uniq, key=lambda q: math.hypot(*q["off"]))
    elif len(zones) >= 3:
        c0 = min(zones, key=lambda q: math.hypot(*q["off"]))
        core = c0 if math.hypot(*c0["off"]) < 0.2 else None
    ref = core["off"] if core is not None else (0.0, 0.0)
    for q in zones:
        if q is core:
            q["side"] = "core"
        else:
            q["side"] = _bearing((q["off"][0] - ref[0]) * (kx1 - kx0), (q["off"][1] - ref[1]) * (ky1 - ky0),
                                 north_deg)
    # two blocks on one side: the farther one keeps the plain side, the nearer gets 'inner'
    for s in {q["side"] for q in zones}:
        same = sorted([q for q in zones if q["side"] == s], key=lambda q: -math.hypot(*q["off"]))
        for k_, q in enumerate(same[1:], 1):
            q["side"] = f"{s} ({k_ + 1})"
    # the main plan: the largest plan view's building extent; key plan extent -> plan extent, x and y scaled apart
    plans = [v for v in vs if v.kind in PLAN_KINDS and v.kind != "key_plan" and v.bbox]
    plan_disp = None
    if plans:
        pv = max(plans, key=lambda v: (v.bbox[2] - v.bbox[0]) * (v.bbox[3] - v.bbox[1]))
        pdsp = pv.display_bbox or _to_disp(path, page, pv.bbox)
        plan_disp = _plan_extent(path, page, pdsp)
    out_z = []
    kb_disp = (ox + kx0 / z, oy + ky0 / z, ox + kx1 / z, oy + ky1 / z)
    for q in zones:
        bx = q["box_px"]
        kdisp = (ox + bx[0] / z, oy + bx[1] / z, ox + bx[2] / z, oy + bx[3] / z)
        pb = None
        if plan_disp:
            sx = (plan_disp[2] - plan_disp[0]) / max(1e-6, kb_disp[2] - kb_disp[0])
            sy = (plan_disp[3] - plan_disp[1]) / max(1e-6, kb_disp[3] - kb_disp[1])
            pb = (plan_disp[0] + (kdisp[0] - kb_disp[0]) * sx, plan_disp[1] + (kdisp[1] - kb_disp[1]) * sy,
                  plan_disp[0] + (kdisp[2] - kb_disp[0]) * sx, plan_disp[1] + (kdisp[3] - kb_disp[1]) * sy)
        side = q["side"]
        name = "core" if side == "core" else f"{side} wing"
        if len(zones) == 1:            # one coloured block: the part of the building this sheet shows (a module /
            side, name = "shown", "the part this sheet shows"   # phase marked on the key plan), not a wing
        out_z.append({"name": name, "side": side, "colour": q["colour"], "share": q["share"],
                      "key_box": _to_page(path, page, kdisp), "plan_box": _to_page(path, page, pb) if pb else None})
    aspect_k = (kb_disp[2] - kb_disp[0]) / max(1e-6, kb_disp[3] - kb_disp[1])
    aspect_p = ((plan_disp[2] - plan_disp[0]) / max(1e-6, plan_disp[3] - plan_disp[1])) if plan_disp else None
    conf = 0.7 if plan_disp and aspect_p and 0.75 <= aspect_k / aspect_p <= 1.33 else 0.4
    if grey:                           # a scan: tones are close and the plan's extent includes its dimension strings
        conf = min(conf, 0.55)
    note = (f"{len(out_z)} zones read from the KEY PLAN's {'grey' if grey else 'colour'} blocks; {north_note}; "
            + ("each zone mapped onto the main plan by fitting the key plan's extent to the plan's building extent"
               + ("" if conf >= 0.7 else " (their shapes differ: check the boxes)") if plan_disp else
               "no main plan view to map the zones onto"))
    return {"found": True, "zones": out_z, "key_box": _to_page(path, page, kb_disp),
            "plan_box": _to_page(path, page, plan_disp) if plan_disp else None, "north": north_deg, "note": note,
            "confidence": conf}


def zone_for(path: str, question: str, page: int = 0, layer=None) -> dict | None:
    """The key-plan zone a question names ('the north wing', 'east block', 'the core') -> the zone dict + 'why', else
    None (no side word, no key plan, or no zone on that side)."""
    m = _Q_SIDE.search(question or "")
    if not m or (not m.group(2) and _norm_side(m.group(1)) != "core"):
        return None
    want = _norm_side(m.group(1))
    kp = key_plan_zones(path, page, layer)
    if not kp.get("found"):
        return None
    hit = [z for z in kp["zones"] if z["side"] == want]
    if not hit and "-" not in want and want != "core":   # 'north wing' when the zones are 'north-west' + 'north-east'
        hit = [z for z in kp["zones"] if z["side"].split(" ")[0].startswith(want) or
               z["side"].split(" ")[0].endswith(want)]
        if len(hit) != 1:
            return None
    if not hit:
        return None
    z = dict(hit[0])
    z["why"] = f"'{m.group(0).strip()}' = the KEY PLAN's {z['name']} ({kp['note']})"
    z["confidence"] = kp["confidence"]
    return z
