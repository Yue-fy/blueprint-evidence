"""Gross floor area of a plan from its drawn outline (VP; T3 21:00: building-level AREA answers were guesses).

`footprint(pdf, page=0, view=None)` -> dict with
    gea_m2      gross EXTERNAL area: everything inside the outer face of the outer walls (the filled outline)
    gia_m2      gross INTERNAL area (RICS GIA / GIFA): the outline minus the outer wall band (its measured thickness)
    wall_m      the outer wall thickness used for gia (measured from the dark ink just inside the outline)
    perimeter_m outline length; polygon (pdf_pt, the evidence: the outline drawn on the sheet); bbox (pdf_pt)
    ratio       the 1:N used and where it came from; view (the plan view measured); parts (separate blocks > 5 m²)
    confidence, method, assumptions, missing

    voids       regions left out of gia: drawn with an X or labelled VOID / OPEN TO BELOW (double-height spaces)
    alt_gea_m2  only when the outline found covers a small part of the drawing: the larger 'every double line is a
                wall' reading (confidence then 0.15; the caller shows both or asks)

How: the plan view (views(); no titled view -> the page outside the title block at the sheet's one scale note x the
paper factor) is rendered at ~5 cm per pixel with every text item blanked. Walls = THICK dark strokes (>= 0.22 m: thin
roof / canopy outlines, hatch, door swings, grid and dimension lines drop out) + thin ink within 0.3 m of them (glazing
in the wall line); window bays up to 4 m closed along the wall line only; closed (~2 m), holes filled, opened (~1.5 m
disc) -> the building's blobs. Line-only CAD (walls = two thin lines): the pairs are closed into bands; bands wider
than 0.9 m (hatched roofs below, stair treads) are left out and added back only where mostly white (rooms).
GIA = the outline eroded by the outer wall thickness measured from the ink, minus the voids. Never runs a model:
~0.3 s per sheet (median of 185 pages), $0.

Measured (VP 21:40, printed GIFA on EFA baseline sheets, not SEALED): solid-wall plans 8 / 8 within 3.5 % (GF
1247.6 / 1248.9 vs 1248, FF 940.3 / 939.9 vs 932 after the hall void, 240-place 1276 vs 1268, 3FE GF +2.7 %, FF
+3.3 / +3.5 %); X1's line-only twins of the same sheets median 3.4 % (11 / 16 incl. solid twins within 5 %, worst -16 %).
Pictures (footprint_answer only): read on P1's cached traced copy <stem>.vec6.pdf (never traces itself), evidence
  x dpi/72 -> image px, confidence <= 0.5: X1's SEN L00 scan PNG 1674.9 and Stoa's DfE PNG 1695.1 vs the vector PDF 1670.8 m².
Known gaps: AFCEC dorm 1:480 floors (dense furniture + colour fills, thin walls) -> low confidence + alt_gea_m2 2,640
(printed 26,562 SF = 2,468 m²); voids drawn without an X or a label are not found; site-scale sheets flagged
implausible (> 30,000 m² on one floor).
"""
from __future__ import annotations

import math
import os
import re

import numpy as np

_PX_M = 0.05            # target metres per pixel
_CLOSE_M = 1.0          # closing radius (bridges openings up to ~2 m)
_OPEN_M = 0.75          # opening radius (removes thin things < ~1.5 m wide)
_MIN_PART_M2 = 5.0
_THICK_M = 0.22         # a wall stroke is at least this thick
_LINE_ONLY_M2 = 1.5     # less thick ink than this in the view: a line-only drawing
_PAIR_M = 0.3           # line-only walls: the two lines of a wall are at most ~2x this apart
_WIDE_M = 0.9           # line-only: a closed band wider than this is a hatched / filled area, not a wall
_VOID_WORDS = re.compile(r"^\s*VOID\b(?!\s+ABOVE)|\bOPEN\s+TO\s+(?:BELOW|HALL)|\bDOUBLE[\s-]+HEIGHT\b|\bVOID\s+OVER\b|"
                         r"^\s*(?:LUFTRAUM|ILMATILA)\b|"
                         # RICS GIA leaves out open external areas (courtyards, terraces, covered ways) when a closed
                         # white region inside the outline holds the label; 'VOID ABOVE' on a lower floor IS floor area
                         r"\bEXTERNAL\s+(?:PLANT|PLAY|AREA)\b|\bPLANT\s+COMPOUND\b|\bCOURTYARD\b|\bOPEN\s+TO\s+SKY\b|"
                         r"\bROOF\s+TERRACE\b|^\s*(?:TERRACE|BALCONY|CANOPY)\b|\bLIGHT\s*WELL\b|"
                         r"\bCOVERED\s+(?:WAY|PLAY|WALKWAY)\b", re.I)
# words written ON a roof drawn below / beside the floor: they do not name a room (DfE SEC Level 02: 'Ventilated
# enclosure' boxes on the hall roof)
_ROOF_WORDS = re.compile(r"\b(?:ROOF\w*|SKY\s*LIGHTS?|PV|SOLAR|PHOTOVOLTAIC|PARAPETS?|GUTTERS?|OUTLETS?|FALLS?|SEDUM|"
                         r"MEMBRANE|AOV|SMOKE\s+VENTS?|VENTILATED|ENCLOSURES?|CRASH\s+DECK|"
                         r"MANSAFE|FALL\s+ARREST|UPSTANDS?|COPING|PLANT\s+(?:SCREEN|ENCLOSURE))\b", re.I)
_MAX_FLOOR_M2 = 30000.0  # one floor larger than this: the scale or the view is wrong
_HATCH_DENSITY = 0.18   # ink fraction above which an area is hatch, not a room
_BAY_M = 4.0           # window bays up to this long are closed along the wall line
_NEAR_M = 0.3           # thin ink this close to a wall belongs to it (glazing in the wall line)


def _disc(r: int):
    import cv2

    r = max(1, int(r))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _fill_holes(m: np.ndarray) -> np.ndarray:
    """Every background region not reachable from the image border becomes foreground (the rooms inside the walls)."""
    import cv2

    h, w = m.shape
    pad = np.zeros((h + 2, w + 2), np.uint8)
    pad[1:-1, 1:-1] = m
    mask = np.zeros((h + 4, w + 4), np.uint8)
    outside = pad.copy()
    cv2.floodFill(outside, mask, (0, 0), 2)
    return (outside[1:-1, 1:-1] != 2).astype(np.uint8)


def _diag_hits(near: np.ndarray, x: int, y: int, w: int, h: int, px_m: float) -> list[float]:
    """How much of each diagonal of the box runs over ink (`near` = ink grown ~0.12 m); the drawn X may end a few pixels
    off the region's corners (wall lines): best of the corners jittered by ~0.15 m (VP c6: a 2.8 x 17 m strip read
    0.31 / 0.55 without it)."""
    d = max(1, round(0.15 / px_m))
    k = max(w, h)
    if k <= 10:
        return [0.0, 0.0]
    mid = slice(int(0.1 * k), int(0.9 * k))        # the corners are walls anyway
    best = []
    for (x0, y0, x1, y1) in ((x, y, x + w - 1, y + h - 1), (x, y + h - 1, x + w - 1, y)):
        b = 0.0
        for dx0 in (-d, 0, d):
            for dx1 in (-d, 0, d):
                xs = np.clip(np.linspace(x0 + dx0, x1 + dx1, k).round().astype(int), 0, near.shape[1] - 1)
                ys = np.clip(np.linspace(y0, y1, k).round().astype(int), 0, near.shape[0] - 1)
                b = max(b, float(near[ys[mid], xs[mid]].mean()))
        best.append(b)
    return best


def _voids(ink: np.ndarray, fp: np.ndarray, px_m: float, labels=()) -> tuple[list, float]:
    """Enclosed regions drawn as a VOID (a double-height space seen from the floor above, a lift / stair shaft): crossed
    corner to corner by an 'X' (both diagonals of the region's box run over ink), or labelled 'VOID' / 'OPEN TO BELOW'
    (`labels`: the label centres in pixels). GIFA leaves them out."""
    import cv2

    k = max(3, round(0.5 / px_m))            # rooms from the horizontal / vertical strokes only: the X's own
    u8 = ink.astype(np.uint8)                 # diagonals must not cut the void into four triangles
    hv = cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((1, k), np.uint8)) | \
        cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((k, 1), np.uint8))
    lines = cv2.morphologyEx(hv, cv2.MORPH_CLOSE, _disc(max(1, round(0.5 / px_m))))
    rooms = (fp.astype(bool) & ~lines.astype(bool)).astype(np.uint8)
    n, lab, st, _c = cv2.connectedComponentsWithStats(rooms, connectivity=4)
    out, tot = [], 0.0
    near = cv2.dilate(ink.astype(np.uint8), _disc(max(1, round(0.12 / px_m)))).astype(bool)
    done: set[int] = set()
    # a LABELLED void on a colour-filled plan is the white box the room fills stop at: the dashed rooflight / beam
    # lines and the X inside it cut the line-bounded region into pieces (EFA secondary 2F corridor voids: 2.9 / 9.6 m²
    # pieces of ~19 m² boxes). Thin ink is passable here; only wide ink (fills, solid walls) bounds the box.
    wide = cv2.morphologyEx(u8, cv2.MORPH_OPEN, _disc(max(1, round(0.25 / px_m)))).astype(bool)
    fpb = fp.astype(bool)
    if wide.any():
        free = (fpb & ~wide).astype(np.uint8)
        nf, labf, stf, _cf = cv2.connectedComponentsWithStats(free, connectivity=4)
        seen: set[int] = set()
        for px, py in labels:
            if not (0 <= py < labf.shape[0] and 0 <= px < labf.shape[1]) or not fpb[py, px]:
                continue
            j = int(labf[py, px])
            if j == 0 or j in seen:
                continue
            x, y, w, h, a = (int(v) for v in stf[j])
            i0 = int(lab[py, px])
            # bounded (not the building's whole interior), box-like, and larger than the line-bounded piece
            if a * px_m * px_m < 5.0 or a > 0.1 * fpb.sum() or a < 0.5 * w * h or (i0 and a <= st[i0, 4]):
                continue
            seen.add(j)
            reg = labf == j
            done |= {int(k) for k in np.unique(lab[reg]) if k}
            m2 = float(reg.sum()) * px_m * px_m
            out.append({"bbox_px": [x, y, x + w, y + h], "m2": round(m2, 1), "diagonals": [], "how": "white box"})
            tot += m2
    def diagonals(x, y, w, h) -> list[float]:
        return _diag_hits(near, x, y, w, h, px_m)

    small = []
    for i in range(1, n):
        if i in done:
            continue
        x, y, w, h, a = (int(v) for v in st[i])
        named = any(0 <= py < lab.shape[0] and 0 <= px < lab.shape[1] and lab[py, px] == i for px, py in labels)
        # a region LABELLED void may be small (narrow corridor voids, 1.8 x 9 m); unlabelled X-crossed ones >= 15 m²
        # (smaller X boxes are lift shafts / risers, which GIA keeps), unless they adjoin a void (below)
        if a * px_m * px_m < 3.0 or w < 8 or h < 8:
            continue
        if a * px_m * px_m < (5.0 if named else 15.0):
            if not named:
                small.append(i)
            continue
        hits = diagonals(x, y, w, h)
        if min(hits) >= 0.6 or named:
            # the void's area to its walls' centre lines: the region grown by half a partition
            reg = cv2.dilate((lab == i).astype(np.uint8), _disc(max(1, round(0.1 / px_m))))
            m2 = float((reg.astype(bool) & fp.astype(bool)).sum()) * px_m * px_m
            out.append({"bbox_px": [x, y, x + w, y + h], "m2": round(m2, 1), "diagonals": [round(h_, 2) for h_ in hits]})
            tot += m2
    # small X-crossed boxes right beside a void are the same void cut by its beams (EFA secondary 1F: 4-9 m² boxes
    # between the hall voids)
    if out and small:
        vmask = np.zeros(lab.shape, np.uint8)
        for v in out:
            x0, y0, x1, y1 = v["bbox_px"]
            vmask[y0:y1, x0:x1] = 1
        vmask = cv2.dilate(vmask, _disc(max(1, round(0.6 / px_m)))).astype(bool)
        for i in small:
            x, y, w, h, a = (int(v) for v in st[i])
            if not vmask[y:y + h, x:x + w].any() or min(diagonals(x, y, w, h)) < 0.6:
                continue
            reg = cv2.dilate((lab == i).astype(np.uint8), _disc(max(1, round(0.1 / px_m))))
            m2 = float((reg.astype(bool) & fp.astype(bool)).sum()) * px_m * px_m
            out.append({"bbox_px": [x, y, x + w, y + h], "m2": round(m2, 1), "diagonals": [], "how": "beside a void"})
            tot += m2
    return out, tot


def _drop_unlabelled(fp: np.ndarray, ink: np.ndarray, px_m: float, word_boxes: list) -> tuple[np.ndarray, float]:
    """A plan names its rooms: a large BOX-LIKE region of the outline with no word in it, on a plan whose other regions
    are named, is a roof below / an external area the wall bands closed in (EFA secondary 2F line-only twin: the sports
    hall roof with its rooflights, 821 m² = +94 %). Narrow regions (corridors) and unlabelled plans are left alone.
    -> (outline, m² left out)."""
    import cv2

    if len(word_boxes) < 5:
        return fp, 0.0
    k = max(3, round(0.5 / px_m))
    u8 = ink.astype(np.uint8)
    hv = cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((1, k), np.uint8)) | \
        cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((k, 1), np.uint8))
    lines = cv2.morphologyEx(hv, cv2.MORPH_CLOSE, _disc(max(1, round(0.5 / px_m))))
    fpb = fp.astype(bool)
    rooms = (fpb & ~lines.astype(bool)).astype(np.uint8)
    n, lab, st, _c = cv2.connectedComponentsWithStats(rooms, connectivity=4)
    named = set()
    for x0, y0, x1, y1 in word_boxes:
        sub = lab[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        named |= {int(v) for v in np.unique(sub) if v}
    if len(named) < 5:
        return fp, 0.0
    total = float(fpb.sum())
    near = cv2.dilate(u8, _disc(max(1, round(0.12 / px_m)))).astype(bool)
    drop = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        if i in named or a * px_m * px_m < 100 or a < 0.03 * total or a < 0.5 * w * h or min(w, h) * px_m < 6.0:
            continue
        if min(_diag_hits(near, x, y, w, h, px_m)) >= 0.6:
            continue                   # crossed by an X: a void (the void reader deducts it from the GIA)
        drop.append(i)
    if not drop:
        return fp, 0.0
    cut = cv2.dilate(np.isin(lab, drop).astype(np.uint8), _disc(max(1, round(0.4 / px_m)))).astype(bool)
    new = (fpb & ~cut).astype(np.uint8)
    new = cv2.morphologyEx(new, cv2.MORPH_OPEN, _disc(round(_OPEN_M / px_m)))
    nn, labn, stn, _cn = cv2.connectedComponentsWithStats(new, connectivity=8)
    if nn <= 1:
        return fp, 0.0
    big = max(stn[i, cv2.CC_STAT_AREA] for i in range(1, nn))
    keep = np.isin(labn, [i for i in range(1, nn) if stn[i, cv2.CC_STAT_AREA] >= max(0.05 * big, _MIN_PART_M2 / px_m ** 2)])
    left = float(keep.sum())
    if left < 0.35 * total:           # the named part keeps at least a third (DfE SEC L02: 1,975 of 4,421 m²; the rest = 2 roofs)
        return fp, 0.0
    return keep, round((total - left) * px_m * px_m, 1)


def _px_box_to_pdf(b, clip, zoom, to_pdf) -> list[float]:
    import pymupdf

    r = pymupdf.Rect(clip.x0 + b[0] / zoom, clip.y0 + b[1] / zoom, clip.x0 + b[2] / zoom, clip.y0 + b[3] / zoom) * to_pdf
    return [round(x, 1) for x in (r.x0, r.y0, r.x1, r.y1)]


def _sheet_scale_and_region(pdf: str, page: int):
    """A sheet with no titled view: its single scale note (x the paper factor: '1:200@A3' on an A3 page = 1:200) and
    the page outside the title block (displayed points)."""
    import importlib

    V = importlib.import_module("stoa.sheet.views")   # (stoa.sheet.views is also the name of a function)

    r = V.page_reading(pdf, page, None)
    if r is None:
        return None, None, None
    lines, _ink, _s, W, H, _a, _b = r
    rs = sorted({sc["ratio"] for l in lines for sc in V._parse_scales(l.text)})
    if len(rs) != 1:
        return None, None, None
    f, why = V._paper_factor(lines, W, H, rs[0])
    zone = V._titleblock_zone(lines, W, H)
    region = (0, 0, W, H)
    if zone:
        region = (0, 0, zone[0], H) if zone[1] == 0 else (0, 0, W, zone[1])
    return rs[0] * f, region, why


def _blank_frame_rules(img: np.ndarray, vbox, margin: float, page_px) -> None:
    """The 4 m pad around a view's extent can reach the sheet's FRAME / title-block rules (USACE A-101F at 1:50: the
    double frame closed into a band and the outline became the whole page, 1,175 m²): rows / columns beyond the view's
    extent (+ `margin` px, 0.5 m: views' extent can be the colour fills, the outer walls just outside) whose ink runs
    along >= 70 % of the PAGE's width / height are such rules, blanked (no wall is that long outside its view)."""
    m = int(round(margin))
    x0, y0, x1, y1 = (int(round(float(t))) for t in vbox)
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    inked = img.min(axis=2) < 235   # frame rules are thin: at ~0.05 m / px they render as light grey
    rows = inked.sum(axis=1) >= 0.7 * page_px[0]
    cols = inked.sum(axis=0) >= 0.7 * page_px[1]
    for y in np.nonzero(rows)[0]:
        if y < y0 or y > y1:
            img[max(0, y - 1):y + 2, :] = 255
    for x in np.nonzero(cols)[0]:
        if x < x0 or x > x1:
            img[:, max(0, x - 1):x + 2] = 255


def _frame_rects(pg) -> list:
    """The sheet FRAME as drawn: unfilled single-rectangle paths that run >= 85 % of the page height or width (the
    drawing frame, title-block strips), kept only when one of them encloses >= 80 % of the page both ways and together
    they come within 6 % of all four page edges. Display coordinates [(x0, y0, x1, y1, stroke width)]. The farmhouse
    A1.2 (1:48): the view's extent takes in its frame, so the 'rules beyond the view' blanking never reached it and the
    outline was the frame's inside, 216 m² (house 47.6 m²). A tight-cropped plan's outer wall is not one unfilled
    rectangle reaching all four edges."""
    try:
        import pymupdf

        W, H = pg.rect.width, pg.rect.height
        rm = pg.rotation_matrix
        if pg.rotation:
            d = pg.rect * rm
            W, H = abs(d.width), abs(d.height)
        cands = []
        for g in pg.get_drawings():
            if g.get("fill") is not None or len(g["items"]) != 1 or g["items"][0][0] not in ("re", "qu"):
                continue
            r = pymupdf.Rect(g["rect"]) * rm if pg.rotation else pymupdf.Rect(g["rect"])
            if r.height >= 0.85 * H or r.width >= 0.85 * W:
                cands.append((r.x0, r.y0, r.x1, r.y1, float(g.get("width") or 0.0)))
        if not any(c[2] - c[0] >= 0.8 * W and c[3] - c[1] >= 0.8 * H for c in cands):
            return []
        ux0, uy0 = min(c[0] for c in cands), min(c[1] for c in cands)
        ux1, uy1 = max(c[2] for c in cands), max(c[3] for c in cands)
        if ux0 > 0.06 * W or uy0 > 0.06 * H or ux1 < 0.94 * W or uy1 < 0.94 * H:
            return []
        return cands
    except Exception:  # noqa: BLE001
        return []


def _reaches_frame(box, frame, zoom: float) -> bool:
    """A display-space box (x0, y0, x1, y1) touching or crossing the largest frame rectangle's sides."""
    main = max(frame, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    tol = 2.0 + 2.0 / zoom
    return box[0] <= main[0] + tol or box[1] <= main[1] + tol or box[2] >= main[2] - tol or box[3] >= main[3] - tol


def _drop_frame_parts(fp: np.ndarray, frame, clip, zoom: float, px_m: float):
    """Drop the outline's parts that reach the drawn frame, when at least one part stays clear of it (else the
    outline is kept whole and flagged later). -> (mask, m² dropped)."""
    import cv2

    n, lab, st, _c = cv2.connectedComponentsWithStats(fp.astype(np.uint8), connectivity=8)
    if n <= 2:
        return fp, 0.0
    off, keep = [], []
    for i in range(1, n):
        x, y, w, h = (int(st[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH,
                                              cv2.CC_STAT_HEIGHT))
        box = (clip.x0 + x / zoom, clip.y0 + y / zoom, clip.x0 + (x + w) / zoom, clip.y0 + (y + h) / zoom)
        (off if _reaches_frame(box, frame, zoom) else keep).append(i)
    if not off or not keep:
        return fp, 0.0
    drop = np.isin(lab, off)
    out = fp.copy()
    out[drop] = 0
    return out, float(drop.sum()) * px_m * px_m


def _blank_frame_rects(img: np.ndarray, rects, clip, zoom: float) -> None:
    """Blank the four sides of each frame rectangle (stroke width + 1 px each side) in the rendered clip."""
    h, w = img.shape[:2]
    for x0, y0, x1, y1, sw in rects:
        b = max(1, int(np.ceil(sw * zoom / 2))) + 1
        X0, Y0 = int(round((x0 - clip.x0) * zoom)), int(round((y0 - clip.y0) * zoom))
        X1, Y1 = int(round((x1 - clip.x0) * zoom)), int(round((y1 - clip.y0) * zoom))
        ya, yb = max(0, Y0 - b), min(h, Y1 + b + 1)
        xa, xb = max(0, X0 - b), min(w, X1 + b + 1)
        for Y in (Y0, Y1):
            if -b <= Y < h + b:
                img[max(0, Y - b):min(h, Y + b + 1), xa:xb] = 255
        for X in (X0, X1):
            if -b <= X < w + b:
                img[ya:yb, max(0, X - b):min(w, X + b + 1)] = 255


def _overall_dims_check(pdf: str, page: int, r: float, bbox) -> tuple[bool, str] | None:
    """The traced outline's width / depth vs the LONGEST dimension string along each side around it (strings measured
    at 1:r within 3 %, text inside the outline's box grown by a quarter). A longest string >= half the outline's extent
    is taken as the overall dimension: outline > 1.08 x it -> (False, 'the outline takes in lines outside the building'
    + the overall rectangle as an upper bound) (USACE A-101F 1:50: 31.6 x 21.6 m traced, 26.8 x 15.6 m printed);
    both sides within -5 / +8 % -> (True, 'agrees'); else None (no overall strings)."""
    from stoa.sheet.scalecheck import MIN_STRINGS, _numbers

    if not r or not bbox or _numbers(str(pdf), int(page)) < MIN_STRINGS:
        return None
    from stoa.features.selfcheck import _dims

    x0, y0, x1, y1 = bbox
    gx, gy = 0.25 * (x1 - x0), 0.25 * (y1 - y0)
    m_pt = r * 0.0254 / 72.0
    ext = {"h": (x1 - x0) * m_pt, "v": (y1 - y0) * m_pt}
    best = {"h": 0.0, "v": 0.0}
    for d in _dims(str(pdf), int(page), float(r)):
        if d["meas"] <= 0 or abs(d["val"] / d["meas"] - 1) > 0.03:
            continue
        b = d["box"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x0 - gx <= cx <= x1 + gx and y0 - gy <= cy <= y1 + gy:
            best[d["o"]] = max(best[d["o"]], d["val"] / 1000.0)
    side = {"h": "wide", "v": "deep"}
    bad, good = [], []
    for o in ("h", "v"):
        D, E = best[o], ext[o]
        if D <= 0 or D < 0.5 * E:
            continue
        if E > 1.08 * D:
            bad.append(f"{E:.1f} m {side[o]} where the longest dimension string says {D:.2f} m")
        elif E >= 0.95 * D:
            good.append(f"{E:.1f} vs {D:.2f} m")
    if bad:
        rect = best["h"] * best["v"]
        up = (f"; the overall dimensions bound it at {best['h']:.2f} x {best['v']:.2f} m = {rect:,.0f} m² (an upper "
              f"bound for the floor)") if best["h"] >= 0.5 * ext["h"] and best["v"] >= 0.5 * ext["v"] else ""
        return False, ("the traced outline is " + " and ".join(bad) + ": it takes in lines outside the building "
                       "(dimension / grid / annotation lines)" + up)
    if len(good) == 2:
        return True, f"outline extent checked on the overall dimension strings ({'; '.join(good)}) ✓"
    return None


def _dims_checked(pdf: str, page: int, r, out: dict):
    """xr-11 on a sheet with no titled view: its one scale note checked on its dimension strings (rescaled at a clear
    paper-size step, as views does); no note at all -> the 1:N the strings measure, when they are clear."""
    try:
        from stoa.sheet.scalecheck import STEPS, dimension_scale

        d = dimension_scale(pdf, page, r)
    except Exception:  # noqa: BLE001
        return r
    if not d:
        return r
    if r and d.get("agrees") is True:
        out["assumptions"].append(d["note"])
    elif r and d.get("agrees") is False:
        step = next((s for s in STEPS[1:] if abs(d["factor"] / s - 1) <= 0.03), None)
        if step:
            out["assumptions"].append(d["note"] + f" -> 1:{r * step:g} used")
            return round(r * step, 2)
        out["assumptions"].append(d["note"] + ": check")
    elif not r and d.get("ratio"):
        out["assumptions"].append(d["note"] + " -> used as the scale")
        out["missing"].append("a printed scale (the scale comes from the dimension strings)")
        return d["ratio"]
    return r


def _scale_bar_boxes(pdf: str, page: int) -> list:
    """The number rows of the page's graphic scale bars (stoa.sheet.views.scale_bars), in displayed points."""
    import importlib

    try:
        V = importlib.import_module("stoa.sheet.views")   # (stoa.sheet.views is also the name of a function)
        r = V.page_reading(pdf, page, None)
        if r is None:
            return []
        boxes = [b["box"] for b in V.scale_bars(r[0])]
        for ln in r[0]:   # the numbers as ONE text line: '0 1 2 3 4 5 6 7 8 9 10' (EFA sheets)
            m = re.fullmatch(r"\s*0((?:\s+\d+(?:\.\d+)?){3,})\s*(?:m|metres|meters|ft|feet)?\s*", ln.text, re.I)
            if m:
                vals = [float(v) for v in m.group(1).split()]
                if all(b > a for a, b in zip([0.0] + vals, vals)):
                    boxes.append(ln.box)
        return [r[6](b) for b in boxes]
    except Exception:  # noqa: BLE001 — no bar read: nothing blanked
        return []


def _outline(thick: np.ndarray, ink: np.ndarray, px_m: float):
    """Wall bands -> the building's filled outline: (mask, kept component ids, stats) or None."""
    import cv2

    near = cv2.dilate(thick, _disc(max(1, round(_NEAR_M / px_m))))
    walls = (thick.astype(bool) | (ink & near.astype(bool))).astype(np.uint8)
    L = max(3, round(_BAY_M / px_m))      # window bays between wall piers: bridged along the wall line only
    m = walls | cv2.morphologyEx(walls, cv2.MORPH_CLOSE, np.ones((1, L), np.uint8)) \
        | cv2.morphologyEx(walls, cv2.MORPH_CLOSE, np.ones((L, 1), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, _disc(round(_CLOSE_M / px_m)))
    m = _fill_holes(m)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, _disc(round(_OPEN_M / px_m)))
    n, lab, stats, _c = cv2.connectedComponentsWithStats(m, connectivity=8)
    min_px = _MIN_PART_M2 / (px_m * px_m)
    parts = [i for i in range(1, n) if stats[i, cv2.CC_STAT_AREA] >= min_px]
    if not parts:
        return None
    big = max(stats[i, cv2.CC_STAT_AREA] for i in parts)
    keep = [i for i in parts if stats[i, cv2.CC_STAT_AREA] >= 0.05 * big]   # separate blocks of one building
    return np.isin(lab, keep), keep, stats


def _plan_view(vs, view=None):
    from stoa.sheet.views import PLAN_KINDS

    if view is not None:
        return view
    plans = [v for v in vs if v.kind in ("floor_plan", "drawing") and v.display_bbox]
    if not plans:
        plans = [v for v in vs if v.kind in PLAN_KINDS and v.display_bbox]
    if not plans:
        return None
    return max(plans, key=lambda v: (v.display_bbox[2] - v.display_bbox[0]) * (v.display_bbox[3] - v.display_bbox[1]))


def _wall_thickness(dark: np.ndarray, fp: np.ndarray, px_m: float) -> tuple[float, list[float]]:
    """Outer wall thickness: the dark-ink fraction of each 1-px ring just inside the outline; the wall ends where the
    fraction drops for good (two-line walls: the inner line within 0.6 m counts)."""
    import cv2

    dist = cv2.distanceTransform(fp.astype(np.uint8), cv2.DIST_L2, 3)
    prof = []
    for i in range(int(0.8 / px_m) + 1):
        ring = (dist > i) & (dist <= i + 1)
        n = int(ring.sum())
        prof.append(float((dark & ring).sum()) / n if n else 0.0)
    # the wall's ink may start a little inside the outline (a cladding / glazing line drawn just outside it: EFA
    # secondary 2F rings 0.07 / 0.18 / 0.22, then 0.27 .. 0.51 up to 0.5 m): any ring within 0.6 m counts (VP c6; was
    # the first 3 rings only -> wall 0 -> GIA = GEA, +11.5 %)
    lim = int(0.6 / px_m)
    if not prof or max(prof[:lim + 1]) < 0.35:
        return 0.0, prof
    last = 0
    for i, f in enumerate(prof[:lim + 1]):
        if f >= 0.35:
            last = i
    return (last + 1) * px_m, prof


def footprint(pdf: str, page: int = 0, view=None, ratio: float | None = None, px_m: float = _PX_M) -> dict:
    """See the module doc. Never raises: a page it cannot read comes back with value None and a `missing` line."""
    try:
        return _footprint(pdf, page, view, ratio, px_m)
    except Exception as e:  # noqa: BLE001 - a broken page must never break a solver
        return {"gea_m2": None, "gia_m2": None, "wall_m": None, "perimeter_m": None, "polygon": [], "polygons": [],
                "bbox": None, "ratio": None, "view": None, "parts": [], "voids": [], "confidence": 0.0,
                "method": "footprint (VP): drawn outline", "assumptions": [],
                "missing": [f"the outline could not be read ({type(e).__name__})"]}


def _footprint(pdf: str, page: int = 0, view=None, ratio: float | None = None, px_m: float = _PX_M) -> dict:
    import cv2
    import pymupdf
    from stoa.sheet.views import views

    out = {"gea_m2": None, "gia_m2": None, "wall_m": None, "perimeter_m": None, "polygon": [], "polygons": [],
           "bbox": None, "ratio": None, "view": None, "parts": [], "voids": [], "confidence": 0.0,
           "method": "footprint (VP): drawn outline",
           "assumptions": [], "missing": []}
    vs = views(pdf, page)
    v = _plan_view(vs, view)
    r = ratio or (v and (v.page_ratio or v.ratio))
    region = v.display_bbox if v is not None and v.display_bbox else None
    if v is not None:
        out["view"] = v.title
        if not ratio:  # xr-11: the scale checked on the sheet's own dimension strings (views._apply_dims)
            out["assumptions"] += [n for n in v.notes if "dimension strings" in n][:1]
    elif not vs:                       # one untitled drawing: the page minus its title block, the sheet's own scale
        r2, region, why = _sheet_scale_and_region(pdf, page)
        r = ratio or r2
        out["view"] = "(the sheet's only drawing)"
        if why:
            out["assumptions"].append(why)
        if not ratio:
            r = _dims_checked(pdf, page, r, out)
    else:
        out["missing"].append("no plan view on this sheet (sections / elevations / details only)")
        return out
    if not r:
        out["missing"].append("the sheet's scale (no 1:N note or scale bar read for the plan)")
        return out
    out["ratio"] = r
    m_per_pt = r * 0.0254 / 72.0
    zoom = m_per_pt / px_m
    with pymupdf.open(pdf) as doc:
        pg = doc[int(page)]
        disp = pg.rect * pg.rotation_matrix if pg.rotation else pg.rect
        clip = pymupdf.Rect(*region) if region else pymupdf.Rect(0, 0, abs(disp.width), abs(disp.height))
        pad = 4.0 / m_per_pt   # 4 m around the view's extent
        page_box = pymupdf.Rect(0, 0, abs(disp.width), abs(disp.height))
        clip = pymupdf.Rect(clip.x0 - pad, clip.y0 - pad, clip.x1 + pad, clip.y1 + pad) & page_box
        if zoom * max(clip.width, clip.height) > 9000:      # very large sheets: coarser pixels
            zoom = 9000 / max(clip.width, clip.height)
        px_m = m_per_pt / zoom
        # render the DISPLAYED page; text boxes are blanked in displayed coordinates
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
        rm = pg.rotation_matrix
        void_pts: list = []
        word_boxes: list = []
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    if not sp["text"].strip():
                        continue
                    bb = pymupdf.Rect(sp["bbox"]) * rm
                    if _VOID_WORDS.search(sp["text"]):
                        void_pts.append((int(((bb.x0 + bb.x1) / 2 - clip.x0) * zoom),
                                         int(((bb.y0 + bb.y1) / 2 - clip.y0) * zoom)))
                    x0, y0 = int((bb.x0 - clip.x0) * zoom) - 1, int((bb.y0 - clip.y0) * zoom) - 1
                    x1, y1 = int((bb.x1 - clip.x0) * zoom) + 2, int((bb.y1 - clip.y0) * zoom) + 2
                    img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 255
                    if re.search(r"[A-Za-z]{2}", sp["text"]) and not _ROOF_WORDS.search(sp["text"]):
                        word_boxes.append((x0, y0, x1, y1))
        frame = _frame_rects(pg) if os.environ.get("STOA_VP_FRAME", "1") != "0" else []
        if frame:   # the drawn sheet frame / title-block strips: never part of a building outline
            _blank_frame_rects(img, frame, clip, zoom)
        to_pdf = ~rm if pg.rotation else pymupdf.Identity
    if region and v is not None:
        _blank_frame_rules(img, (np.array(region, dtype=float) - [clip.x0, clip.y0, clip.x0, clip.y0]) * zoom,
                           0.5 / px_m, (page_box.width * zoom, page_box.height * zoom))
    # graphic scale bars: a black / white checkerboard reads as thick wall ink and joins the outline when the bar sits
    # near the building (EFA 3FE 630 GF: +45 m² under the kitchen, +2.7 %): blanked around their number row
    for bx in _scale_bar_boxes(pdf, page):
        hh = min(bx[2] - bx[0], bx[3] - bx[1])
        x0, y0 = int((bx[0] - 3 * hh - clip.x0) * zoom), int((bx[1] - 3 * hh - clip.y0) * zoom)
        x1, y1 = int((bx[2] + 3 * hh - clip.x0) * zoom), int((bx[3] + 3 * hh - clip.y0) * zoom)
        if x1 > 0 and y1 > 0:
            img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 255
    ink = img.min(axis=2) < 235
    # grey lines / hatch (unsaturated, darker than light fills): not the colour fills of rooms
    line_ink = (img.min(axis=2) < 215) & ((img.max(axis=2).astype(int) - img.min(axis=2)) < 30)
    dark = img.max(axis=2) < 120
    # walls = THICK dark strokes (>= ~0.25 m): thin lines (roof / canopy outlines, hatch, door swings, grid, dimension
    # lines, furniture) drop out; thin ink right next to a wall (glazing drawn in the wall line) bridges window gaps.
    # Line-only CAD (walls = two thin lines): the pairs are closed into bands first. The solid reading is tried first;
    # when its outline holds less than a fifth of the drawing's ink extent (a leak through thin walls), the line one.
    thick = cv2.morphologyEx(dark.astype(np.uint8), cv2.MORPH_OPEN, _disc(max(1, round(_THICK_M / 2 / px_m))))
    ys, xs = np.nonzero(ink)
    extent = float((xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)) if len(xs) else 0.0
    line_only = thick.sum() * px_m * px_m < _LINE_ONLY_M2
    full_px = 0.0
    got = None if line_only else _outline(thick, ink, px_m)
    if got is None or got[0].sum() < 0.2 * extent:
        band = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_CLOSE, _disc(max(1, round(_PAIR_M / px_m))))
        # a closed band wider than ~0.9 m is a hatched roof below / stair treads / WC cubicles, not a wall: read the
        # outline without them (A) and with them (B); B's extra parts count only when mostly white (rooms the cut
        # let leak), never when dense ink (hatch)
        wide = cv2.morphologyEx(band, cv2.MORPH_OPEN, _disc(max(1, round(_WIDE_M / 2 / px_m))))
        k_t = _disc(max(1, round(_THICK_M / 2 / px_m)))
        thick2 = cv2.morphologyEx(band & ~cv2.dilate(wide, _disc(1)), cv2.MORPH_OPEN, k_t)
        got2 = _outline(thick2, ink, px_m)
        gotb = _outline(cv2.morphologyEx(band, cv2.MORPH_OPEN, k_t), ink, px_m)
        if gotb is not None:
            full_px = float(gotb[0].sum())
        if gotb is not None:
            base = got2[0] if got2 is not None else np.zeros_like(gotb[0])
            extra = (gotb[0] & ~base).astype(np.uint8)
            n_e, lab_e, st_e, _ce = cv2.connectedComponentsWithStats(extra, connectivity=8)
            add = np.zeros_like(extra, dtype=bool)
            for j in range(1, n_e):
                reg = lab_e == j
                if float(line_ink[reg].mean()) < _HATCH_DENSITY:    # line ink only: colour fills are rooms
                    add |= reg
            if add.any():
                got2 = (base | add, gotb[1], gotb[2])
        if got2 is not None and (got is None or got2[0].sum() > got[0].sum()):
            got, thick = got2, thick2
            out["assumptions"].append("walls drawn as pairs of thin lines: read as wall bands (lone lines such as "
                                      "canopy outlines and door swings left out)")
    line_mode = any("pairs of thin lines" in a for a in out["assumptions"])
    if got is None:
        out["missing"].append("no closed building outline found in the plan view")
        return out
    fp = got[0]
    if frame:   # parts running to the drawn frame are the legend / title-block boxes (Swiss 1805 sheets: +24 m²)
        fp, off_m2 = _drop_frame_parts(fp, frame, clip, zoom, px_m)
        if off_m2:
            out["assumptions"].append(f"left out {off_m2:,.0f} m² outside the building: part(s) running to the "
                                      "sheet's drawn frame (legend / title-block boxes)")
    fp, cut_m2 = _drop_unlabelled(fp, ink, px_m, word_boxes)
    if cut_m2:
        out["assumptions"].append(f"left out {cut_m2:,.0f} m² of the outline: box-like region(s) with no room name in "
                                  "them on a labelled plan (a roof below / an external area)")
    n_p, _lab_p, st_p, _c_p = cv2.connectedComponentsWithStats(fp.astype(np.uint8), connectivity=8)
    part_m2 = sorted((float(st_p[i, cv2.CC_STAT_AREA]) * px_m * px_m for i in range(1, n_p)), reverse=True)
    dark = thick.astype(bool) if thick.sum() else dark
    voids, void_m2 = _voids(ink, fp, px_m, void_pts)
    gea = float(fp.sum()) * px_m * px_m
    t, prof = _wall_thickness(dark, fp, px_m)
    if t > 0:
        er = cv2.erode(fp.astype(np.uint8), _disc(max(1, round(t / px_m))))
        gia = float(er.sum()) * px_m * px_m
    else:
        gia = gea
    gia -= void_m2
    cs, _h = cv2.findContours(fp.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    per = sum(cv2.arcLength(c, True) for c in cs) * px_m
    polys = []
    for c in sorted(cs, key=cv2.contourArea, reverse=True):
        ap = cv2.approxPolyDP(c, max(1.0, 0.15 / px_m), True).reshape(-1, 2)
        pts = []
        for x, y in ap:
            p = pymupdf.Point(clip.x0 + (x + 0.5) / zoom, clip.y0 + (y + 0.5) / zoom) * to_pdf
            pts.append([round(p.x, 1), round(p.y, 1)])
        polys.append(pts)
    ys, xs = np.nonzero(fp)
    b = pymupdf.Rect(clip.x0 + xs.min() / zoom, clip.y0 + ys.min() / zoom, clip.x0 + (xs.max() + 1) / zoom,
                     clip.y0 + (ys.max() + 1) / zoom) * to_pdf
    out.update(gea_m2=round(gea, 1), gia_m2=round(gia, 1), wall_m=round(t, 2), perimeter_m=round(per, 1),
               polygon=polys[0] if polys else [], bbox=[round(x, 1) for x in (b.x0, b.y0, b.x1, b.y1)],
               parts=[round(a, 1) for a in part_m2 if a >= _MIN_PART_M2], polygons=polys,
               voids=[{"m2": vd["m2"], "bbox": _px_box_to_pdf(vd["bbox_px"], clip, zoom, to_pdf)} for vd in voids],
               confidence=0.35 if line_mode else 0.6)
    if frame:   # a framed sheet's building lies inside its frame: an outline reaching the frame was closed by it or
        # by annotation lines out to it (farmhouse A1.2 1:48: the frame's inside 216 m², then a 194 m² blob of
        # dimension chains + section marks out to the page edge; the house is 47.6 m²)
        if _reaches_frame((clip.x0 + xs.min() / zoom, clip.y0 + ys.min() / zoom, clip.x0 + (xs.max() + 1) / zoom,
                           clip.y0 + (ys.max() + 1) / zoom), frame, zoom):
            out["confidence"] = min(out["confidence"], 0.1)
            out["missing"].append("the traced outline reaches the sheet's drawn frame, so lines outside the building "
                                  "(the frame, dimension / annotation lines) closed it: it is not the building's "
                                  "outline; check it on the sheet")
    if fp.sum() < 0.2 * extent and full_px > 3 * fp.sum():
        alt = full_px * px_m * px_m
        out["alt_gea_m2"] = round(alt, 1)
        out["confidence"] = 0.15
        out["missing"].append(f"the outline found ({gea:,.0f} m²) covers a small part of the drawing; reading every "
                              f"double line as a wall gives {alt:,.0f} m² (may include roofs below / hatched areas): "
                              f"check the outline on the sheet")
    if gea > _MAX_FLOOR_M2:
        out["confidence"] = 0.1
        out["missing"].append(f"{gea:,.0f} m² is implausible for one floor: check the scale (1:{r:g}) and that this "
                              f"is a floor plan, not a site plan")
    try:  # xr-11 / H NEXT 1b: the outline's extent checked on a second length, the sheet's overall dimension strings
        chk = _overall_dims_check(pdf, int(page), r, out["bbox"])
    except Exception:  # noqa: BLE001
        chk = None
    if chk is not None:
        if chk[0]:
            out["assumptions"].append(chk[1])
        else:
            out["confidence"] = min(out["confidence"], 0.3)
            out["missing"].append(chk[1])
    out["assumptions"] += [f"scale 1:{r:g} ({'the plan view' if v is not None else 'the sheet'})",
                           "gross internal area = the drawn outline minus the outer wall band "
                           f"({t:.2f} m, measured from the wall ink)" if t else
                           "outer wall thickness not measurable from the ink: internal = external outline",
                           (f"voids drawn with an X (double-height spaces / shafts) deducted: {len(voids)} = {void_m2:.1f} m²"
                            if voids else "no void drawn with an X found (none deducted)")]
    return out


# ----------------------------------------------------------------------------------------------------------------------
# printed gross totals (the sheet's own numbers win; the measured outline cross-checks them)

_AREA_VAL = re.compile(r"(?<![\d.,])(\d{1,3}(?:[ ,]\d{3})+|\d+)(?:\.(\d+))?\s*"
                       r"(m\s?2\b|m²|sq\.?\s?m\b|sqm\b|"
                       r"(?:net\s+|gross\s+)?(?:sf\b|gsf\b|nsf\b|sq\.?\s?ft\b|ft2\b|ft²))", re.I)
_P_TARGET = re.compile(r"\btarget\b|\bbudget\b|\brequired\b|\ballowable\b|\bmax(?:imum)?\b|\bmin(?:imum)?\b", re.I)
_P_PART = re.compile(r"\b(?:wings?|modules?|groups?|common|sub-?total|zones?|blocks?|stairs?|units?|apartments?|rooms?|"
                     r"suites?|department|program|cores?|plant|circulation|corridors?|toilets?|external|site|plot|"
                     r"landscape|playground|hard\s+court|grass)\b", re.I)
_P_NET = re.compile(r"\bnet\b|\bnsf\b|\bnia\b|\busable\b", re.I)
_P_TOTAL = re.compile(r"\btotal\b|\boverall\b|\bgross\b|\bgifa\b|\bgia\b|\bgea\b|\bgfa\b|\bgsf\b|\bactual\s+area\b|"
                      r"\bfloor\s+area\b|\bbuilding\s+area\b|\bbgf\b|\bbrutto", re.I)
_P_HEAD = re.compile(r"\b(?:gifa|gia|gea|gfa|gross|floor\s+areas?|areas?\s+schedule|accommodation\s+areas?|bgf)\b",
                     re.I)
_P_SHORT_STOREY = {"ground": "0", "gf": "0", "grd": "0", "lower ground": "-1", "lg": "-1", "basement": "-1",
                   "first": "1", "ff": "1", "1st": "1", "second": "2", "2nd": "2", "third": "3", "3rd": "3",
                   "fourth": "4", "4th": "4", "mezzanine": "mezzanine", "roof": "roof"}
_WHOLE_Q = re.compile(r"\b(?:whole|entire|overall)\s+(?:building|school|house|project|facility)\b|"
                      r"\bboth\s+(?:floors|levels|storeys|stories)\b|"
                      r"\ball\s+(?:the\s+)?(?:floors|levels|storeys|stories)\b|\bevery\s+(?:floor|level)\b|"
                      r"\b(?:of|for)\s+the\s+(?:building|school|house|facility|dormitory|dorm)\s*[?.!]?\s*$|"
                      r"\bbuilding'?s?\s+(?:gross|total|gifa|gia|gea)\b", re.I)


def _p_storey(label: str) -> str | None:
    """'Ground:' / 'GF Area' / 'TOTAL SECOND FLOOR =' / 'Level 01' -> storey key as stoa.sheet.sets numbers them."""
    from stoa.sheet.sets import _storey_of

    k, _ = _storey_of(label)
    if k is not None:
        return k
    low = re.sub(r"[^a-z0-9 ]+", " ", label.lower())
    for w in sorted(_P_SHORT_STOREY, key=len, reverse=True):
        if re.search(rf"(?:^|\s){re.escape(w)}(?:\s|$)", low):
            return _P_SHORT_STOREY[w]
    return None


def _area_value(m) -> tuple[float, str]:
    v = float(re.sub(r"[ ,]", "", m.group(1)) + ("." + m.group(2) if m.group(2) else ""))
    u = m.group(3).lower()
    return v, ("m2" if u.startswith(("m", "sq m", "sq.m", "sqm")) or "m²" in u or re.match(r"sq\.?\s?m", u) else "ft2")


def printed_gross(path: str, page: int = 0, layer=None) -> list[dict]:
    """Printed floor-area totals on a sheet: [{value, unit, label, kind ('storey' | 'total' | 'part' | 'target'),
    storey, net, bbox (page space: pdf_pt / image px), text}]. Reads 'Ground: 1248m²' (a label line beside a value
    line on the same row, EFA 'Overall GIFA' blocks), 'TOTAL SECOND FLOOR = 26,562 SF', 'GF Area 1645m2', 'Actual
    Area 1268m2'. A room tag ('Kitchen A: 63 m2') is not a row: the label must name a storey or a total. Images need
    the caller's (cached) text layer. Never raises."""
    from stoa.sheet.views import page_reading

    try:
        r = page_reading(path, page, layer=layer)
    except Exception:  # noqa: BLE001
        return []
    if not r:
        return []
    lines = r[0]
    to_src = r[5]
    out, used = [], set()
    for i, l in enumerate(lines):
        t = l.text.strip()
        if len(t) > 90:
            continue
        m = None
        for m in _AREA_VAL.finditer(t):
            pass
        if m is None:
            continue
        tail = re.sub(r"\([^)]*\)|\*", "", t[m.end():])
        if len(re.sub(r"[^A-Za-z]", "", tail)) >= 3:   # 'FF 55m2 Classroom' (an option note), not a total
            continue
        own = t[:m.start()].strip(" :=-–\t")
        label, box = own, l.box
        if len(re.sub(r"[^A-Za-z]", "", own)) < 2:   # a bare value line: the label is the line to its left on its row
            best = None
            for j, o in enumerate(lines):
                if j == i or o.box[2] > l.box[0] + 2 or not re.search(r"[A-Za-z]{2}", o.text):
                    continue
                ov = min(o.box[3], l.box[3]) - max(o.box[1], l.box[1])
                gap = l.box[0] - o.box[2]
                if ov < 0.5 * min(o.h, l.h) or gap > 14 * l.h:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, j, o)
            if best is None:
                continue
            o = best[2]
            label = o.text.strip(" :=-–\t")
            box = (min(o.box[0], l.box[0]), min(o.box[1], l.box[1]), max(o.box[2], l.box[2]), max(o.box[3], l.box[3]))
            used.add(best[1])
        if len(label) > 60 or _AREA_VAL.search(label):
            continue
        storey = _p_storey(label)
        is_total = bool(_P_TOTAL.search(label)) or bool(_P_TARGET.search(label) and re.search(r"\barea\b", label, re.I))
        if storey is None and not is_total:
            continue
        heads = [h for h in lines if _P_HEAD.search(h.text) and len(h.text) <= 40 and
                 0 <= l.box[1] - h.box[3] <= 10 * l.h and min(h.box[2], box[2]) - max(h.box[0], box[0]) > -2 * l.h]
        head = min(heads, key=lambda h: l.box[1] - h.box[3]).text if heads else ""
        if not is_total and not re.search(r"\b(?:floor|level|storey|story|area)\b", label, re.I) and not head:
            continue   # 'Ground:' / 'First:' alone: only under a GIFA / gross / floor areas heading (EFA blocks)
        if re.fullmatch(r"(?i)\s*total\s*", label) and not head:
            continue   # a bare 'Total' under a room schedule is a NET sum of rooms, not a gross floor area
        v, unit = _area_value(m)
        if v <= 0:
            continue
        kind = ("target" if _P_TARGET.search(label) else
                "part" if _P_PART.search(label) and not re.search(r"\bfloor\b", label, re.I) else
                "storey" if storey is not None else "total")
        src = to_src(box)
        out.append({"value": v, "unit": unit, "label": label, "kind": kind, "storey": storey,
                    "net": bool(_P_NET.search(label) or _P_NET.search(m.group(3))), "bbox": [round(c, 1) for c in src],
                    "head": head, "text": f"{(head + ' ') if head and head.lower() not in label.lower() else ''}"
                                          f"{label}: {m.group(0).strip()}"})
    return out


def _sheet_storey(path: str, page: int, layer=None) -> str | None:
    try:
        from stoa.sheet.sets import classify

        return classify(path, page, layer=layer).storey
    except Exception:  # noqa: BLE001
        return None


# G1 01:58: 'What is the total GIFA?' on the EFA sheet (block Ground 4055 / First 2302 / Second 2253 / Total 8610) took
# this sheet's storey (2302). 'total / overall / combined' + no storey named + not bound to this plan / floor / sheet
# -> the printed Total row, when the sheet also prints storey rows and the total is at least the largest of them.
_TOTAL_Q = re.compile(r"\b(?:total|overall|combined|sum|altogether|in\s+total|grand)\b", re.I)
_THIS_Q = re.compile(r"\b(?:this|that|the\s+(?:current|shown|present))\s+(?:whole\s+|entire\s+)?(?:plan|floor|level|"
                     r"storey|story|sheet|drawing|page|view)\b|\b(?:on|of|for|in)\s+the\s+(?:plan|floor\s+plan|sheet|"
                     r"drawing)\b|\bper\s+(?:floor|level|storey)\b|\beach\s+(?:floor|level|storey)\b", re.I)


ONE_FLOOR_WHY = "the question asks for the whole building; the sheet prints only this floor's total"


def _asked_total_row(question: str, gross: list[dict]) -> dict | None:
    if os.environ.get("STOA_VP_TOTAL_ROW", "1") == "0" or not _TOTAL_Q.search(question or "") or \
            _THIS_Q.search(question or ""):
        return None
    floors = [r for r in gross if r["kind"] == "storey" and r["storey"] is not None]
    tots = [r for r in gross if r["kind"] == "total" and r["storey"] is None]
    if len({r["storey"] for r in floors}) < 2 or not tots:
        return None
    units = {r["unit"] for r in floors}
    tots = [t for t in tots if t["unit"] in units and t["value"] >= max(r["value"] for r in floors if r["unit"] ==
                                                                            t["unit"]) * 1.001]
    if not tots:
        return None
    return max(tots, key=lambda r: r["value"])


def pick_printed(rows: list[dict], question: str, sheet_storey: str | None, measured_m2=()) -> tuple[dict | None, str]:
    """The printed row the question means, and why: a storey the question names; 'whole building' -> the total;
    else this sheet's storey; else the row the measured outline matches (within 8 %); else the only gross row."""
    from stoa.sheet.sets import _storey_of

    gross = [r for r in rows if r["kind"] in ("storey", "total") and not r["net"]]
    if not gross:
        return None, "no printed gross total"
    ext_q = bool(_EXTERNAL_Q.search(question))
    int_q = bool(re.search(r"\bgifa\b|\bgia\b|gross\s+internal", question, re.I))
    basis_ok = [r for r in gross if not (ext_q and re.search(r"\bgifa\b|\bgia\b|internal", r["text"], re.I)) and
                not (int_q and re.search(r"\bgea\b|external", r["text"], re.I))]
    if not basis_ok:
        return None, ("the sheet prints a gross INTERNAL area; the question asks for the external / footprint area"
                      if ext_q else "the sheet prints a gross external area; the question asks for the internal one")
    gross = basis_ok
    q_storey = _storey_of(question)[0]
    if _WHOLE_Q.search(question) and q_storey is None:
        tot = [r for r in gross if r["kind"] == "total" and r["storey"] is None]
        if tot:
            return max(tot, key=lambda r: r["value"]), "the question asks for the whole building: the printed total"
        own = [r for r in gross if sheet_storey is not None and r["storey"] == sheet_storey]
        if own and os.environ.get("STOA_VP_ONE_FLOOR_ROW", "1") != "0":
            # AFCEC dorm A-1..A-3 print only their own 'TOTAL FIRST FLOOR = 28,770 SF': that row (one floor, said)
            # beats a measured outline of the same floor (84 m² @0.15 on the 1:480 half-size print)
            return own[0], ONE_FLOOR_WHY
        return None, "the question asks for the whole building; the sheet prints no building total"
    tot_row = _asked_total_row(question, gross) if q_storey is None else None
    if tot_row is not None:
        return tot_row, "the question asks for the total; the sheet prints a total row over its floors"
    want = q_storey if q_storey is not None else sheet_storey
    if want is not None:
        hit = [r for r in gross if r["storey"] == want]
        if hit:
            return hit[0], ("the storey the question names" if q_storey is not None else "this sheet's storey")
        if q_storey is not None:
            return None, "the sheet prints no total for the storey asked"

    def m2(r):
        return r["value"] if r["unit"] == "m2" else r["value"] / 10.7639

    for mv in measured_m2:
        if not mv:
            continue
        near = sorted(gross, key=lambda r: abs(m2(r) / mv - 1))
        if near and abs(m2(near[0]) / mv - 1) <= 0.08:
            return near[0], "the printed row the measured outline matches"
    floors = [r for r in gross if r["storey"] is not None]
    if not floors and len({r["value"] for r in gross}) == 1:
        return gross[0], "the sheet's only printed gross area"
    return None, "several printed totals; none is this sheet's storey"


# ----------------------------------------------------------------------------------------------------------------------
# the contract hook (for G1's router / T3's per-level building totals)

_GROSS_Q = re.compile(
    r"\b(?:gifa|gia|gea|gross\s+(?:internal\s+|external\s+)?(?:floor\s+)?area|built[\s-]+(?:up\s+)?area|"
    r"(?:building|house|school)(?:'s)?\s+footprint|footprint(?=\s*(?:[?.!]|$)|\s+(?:area\s+)?of\s+(?:the|this)\s+"
    r"(?:whole\s+)?(?:building|house|school|home|block|plan)\b)|"
    r"(?:total|whole|overall|entire|complete)\s+(?:gross\s+)?floor\s*area(?=\s*(?:[?.!]|$)|\s+(?:on|of|for|in)\s+"
    r"(?:(?:the|this)\s+(?:whole\s+|entire\s+)?(?:plan|floor|level|storey|story|building|school|sheet|drawing|"
    r"ground\s+floor|first\s+floor|second\s+floor|third\s+floor|upper\s+floor|lower\s+floor)\b|"
    r"(?:level|floor|storey)\s+\w+\s*(?:[?.!]|$)))|"
    r"floor\s*area\s+of\s+(?:the|this)\s+(?:whole\s+|entire\s+)?(?:plan|floor|level|storey|story|building|school|sheet|"
    r"drawing|ground\s+floor|first\s+floor|second\s+floor|upper\s+floor)|"
    r"(?:how\s+(?:big|large)|what\s+size)\s+is\s+(?:the|this)\s+(?:whole\s+)?(?:building|floor|level|storey|"
    r"ground\s+floor|first\s+floor|second\s+floor)\b|"
    r"area\s+of\s+(?:the|this)\s+(?:whole\s+|entire\s+)?(?:building|floor\s+plan|level|storey)\b|"
    # VP c5: 'total area of this plan', 'area of the whole floor', 'area of level 01', 'square footage', 'how many m2 is
    # the building', 'floor area of the house' - the noun must END the question (not 'area of the ground floor kitchen')
    r"area\s+of\s+(?:(?:the|this)\s+(?:whole\s+|entire\s+)?(?:plan|drawing|sheet|floor|building|house|home|school|"
    r"level|storey|story|basement|(?:ground|first|second|third|upper|lower|top)\s+floor)(?:\s+plan)?|"
    r"(?:level|floor|storey)\s+\w+)\s*[?.!]?\s*$|"
    r"square\s+footage(?:\s+of\s+(?:the|this)\s+(?:whole\s+|entire\s+)?(?:plan|drawing|floor|building|house|level|"
    r"storey|(?:ground|first|second|third)\s+floor)(?:\s+plan)?)?\s*[?.!]?\s*$|"
    r"how\s+many\s+(?:square\s+(?:met(?:re|er)s|feet|foot)|m2|m²|sq\.?\s*(?:m|ft)|sf)\s+(?:is|are|does|in)\s+(?:the|this)"
    r"\s+(?:whole\s+|entire\s+)?(?:floor|building|plan|house|level|storey|drawing)(?:\s+plan)?\b|"
    r"floor\s*area\s+of\s+(?:the|this)\s+(?:house|home|dwelling)\s*[?.!]?\s*$|"
    r"brutto(?:grund)?fl(?:ä|ae)che|\bbgf\b|\bkerrosala|\bbruttoala)", re.I)
_ROOMS_Q = re.compile(r"\b(?:all|every|each)\s+(?:the\s+)?(?:rooms?|spaces?|classrooms?|offices?)\b|\bnet\b|\bnia\b|"
                      r"\busable\b|\bcarpet\b", re.I)
_EXTERNAL_Q = re.compile(r"\bgea\b|\bgross\s+external|\bfootprint\b|\bbuilt[\s-]+(?:up\s+)?area\b|\boutside\s+face", re.I)
# S0 23:55 (d30 wild-dev loss x1-140-farm): 'the footprint of the house WITHOUT the porches' = 216.1 m² from the whole
# outline (gold 47.57): the outline cannot leave named parts out, so such questions go to the other readers
_EXCEPT_Q = re.compile(r"\b(?:without|excluding|excluded|exclude|except|not\s+(?:including|counting)|minus|apart\s+from|"
                       r"other\s+than|but\s+not|ohne|ilman|utan|exkl)\b", re.I)
_SQFT_Q = re.compile(r"\b(?:sq\.?\s*ft|square\s+f(?:ee|oo)t|sf|ft2|ft²)\b", re.I)


# O1 02:24: a whole-FLAT question ('What is the total floor area of the apartment?'); footprint answers it only from the
# flat's printed headline ('2 H+KK+S 45.5 m²'), never from a measured outline (other readers sum the rooms)
_DWELLING_Q = re.compile(
    r"(?:(?:total|whole|overall|entire|complete|gross)\s+)?(?:(?:floor|living|internal)\s+)?(?:area|size|square\s+"
    r"(?:footage|met(?:re|er)s|feet))\s+(?:of|in)\s+(?:the|this)\s+(?:whole\s+|entire\s+)?(?:apartment|flat|dwelling|"
    r"home|unit)(?:\s+in\s+(?:square\s+\w+|m2|m²|sq\.?\s*\w+))?\s*[?.!]?\s*$|how\s+(?:big|large)\s+is\s+(?:the|this)\s+"
    r"(?:whole\s+)?(?:apartment|flat|dwelling)(?:\s+in\s+(?:square\s+\w+|m2|m²|sq\.?\s*\w+))?\s*[?.!]?\s*$|\b(?:the|this)\s+(?:apartment|flat|dwelling)(?:'s)?\s+(?:total\s+)?(?:floor\s+|living\s+)?(?:area|size)"
    r"\b|\b(?:asunnon|huoneiston)\s+(?:koko|(?:kokonais)?pinta-?ala|asuinpinta-?ala|huoneistoala)\b|\bhuoneistoala\b|"
    r"\basuinpinta-?ala\b|\blägenhetens\s+(?:yta|area|storlek|boyta)\b|\bbostadsyta\b|\bwohnfläche\b|"
    r"\bgröße\s+der\s+wohnung\b", re.I)


def is_gross_area_question(question: str) -> bool:
    """'What is the total floor area of this plan?', 'GIFA of level 01', 'How big is the building?' -> True;
    a named room ('floor area of the kitchen'), net / all-rooms sums ('total area of all rooms') -> False.
    A whole-flat question ('How big is the apartment?') -> True: footprint_answer then reads only a printed headline."""
    q = question or ""
    hit = bool(_GROSS_Q.search(q)) or (bool(_DWELLING_Q.search(q)) and os.environ.get("STOA_VP_DWELLING", "1") != "0")
    return hit and not _ROOMS_Q.search(q) and not _EXCEPT_Q.search(q)


def _dwelling_answer(path: str, question: str, page: int = 0):
    """The flat's printed headline ('2 H+KK+S 45.5 m²': Finnish type code = 2 rooms + kitchenette + sauna, and the
    flat's area; Swedish '2 rok') for a whole-flat / house question, cross-checked by the sum of the room tags.
    None when the sheet prints no headline, or several (a block of flats)."""
    from stoa.contract import Answer, Evidence
    from stoa.sheet.labels import room_tags

    img = not str(path).lower().endswith(".pdf")
    if img:
        from stoa.sheet.sets import _cached_layer

        lay = _cached_layer(path)
        tags = room_tags(path, 0, layer=lay) if lay is not None else []
    else:
        tags = room_tags(path, page)
    heads = [t for t in tags if t.dwelling]
    if len({(t.value, t.unit) for t in heads}) != 1:
        return None
    hd = heads[0]
    pv, pu = hd.value, hd.unit
    if _SQFT_Q.search(question) and pu == "m2":
        value, unit = round(pv * 10.7639, 0), "ft2"
    elif _M2_Q.search(question) and pu == "ft2":
        value, unit = round(pv / 10.7639, 1), "m2"
    else:
        value, unit = pv, pu
    rooms = [t for t in tags if not t.dwelling and t.unit == pu]
    tot = round(sum(t.value for t in rooms), 2)
    named = sum(1 for t in rooms if t.name or t.number)
    u = "m²" if pu == "m2" else "sq ft"
    printed = f"{hd.name} {hd.text}"
    assumptions = [f"read from the sheet: the flat's headline '{printed}' (type code {hd.name}: the whole flat, "
                   "its printed area)"]
    trace = [f"printed (VP): '{printed}' (the flat's headline: type code + area)"]
    top = 0.8 if not img else 0.75
    if len(rooms) >= 3 and abs(tot / pv - 1) <= 0.02:
        chk = f"its {len(rooms)} room tags add up to {tot:,g} {u} ({(tot / pv - 1) * 100:+.1f} %: agrees)"
        conf, missing = top, []
    else:
        chk = (f"its {len(rooms)} room tags add up to {tot:,g} {u}: differs, some rooms may carry no tag" if rooms
               else "no room tags to cross-check it")
        conf, missing = top - 0.15, ["room tags that add up to the printed flat area (cross-check)"]
    assumptions.append(chk)
    trace.append(f"cross-check: {chk} ({named} named)")
    if pu != unit:
        assumptions.append(f"converted from the printed {pv:,g} {u}")
    space = "image_px" if img else "pdf_pt"
    ev = [Evidence(file=path, page=None if img else page, bbox=[round(c, 1) for c in hd.box], bbox_space=space,
                   exact=not img, note=f"printed: {printed}")]
    ev += [Evidence(file=path, page=None if img else page, bbox=[round(c, 1) for c in t.area_box], bbox_space=space,
                    exact=not img, note=f"{t.label}: {t.text}") for t in rooms[:40]]
    return Answer(value=value, unit=unit, confidence=round(conf, 2), evidence=ev, solver="footprint", trace=trace,
                  assumptions=assumptions, missing=missing)


def footprint_answer(path: str, question: str, page: int = 0):
    """A contract Answer for a whole-floor / gross area question on a PDF page or a picture, or None (not such a
    question, nothing read). A PRINTED gross total of the floor the question means ('Ground: 1248 m²' in a GIFA
    block, 'TOTAL SECOND FLOOR = 26,562 SF') is the answer, the drawn outline its cross-check; else the outline:
    value = GIA (GIFA, RICS) unless the question asks for the external / footprint area; the other figure, the wall
    thickness, the voids and the scale are stated; evidence = the printed line and / or the outline polygon."""
    import time

    if not is_gross_area_question(question):
        return None
    dq = bool(_DWELLING_Q.search(question)) or bool(re.search(r"\b(?:house|home|dwelling)\b", question, re.I))
    if dq and os.environ.get("STOA_VP_DWELLING", "1") != "0":
        try:
            d = _dwelling_answer(str(path), question, int(page))
        except Exception:  # noqa: BLE001
            d = None
        if d is not None:
            return d
        if not _GROSS_Q.search(question):
            return None   # a whole-flat question with no printed headline: the room readers sum the rooms
    if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp")):
        return _image_answer(str(path), question)
    if not str(path).lower().endswith(".pdf"):
        return None
    t0 = time.time()
    ans, r = _measured_answer(path, question, page)
    try:
        rows = printed_gross(path, page)
    except Exception:  # noqa: BLE001
        rows = []
    try:
        out = _with_printed(ans, r, rows, str(path), int(page), question,
                            _sheet_storey(str(path), int(page)) if rows else None, "pdf_pt")
    except Exception:  # noqa: BLE001 - never break the caller: the measured reading alone
        out = ans
    if out is not None:
        _one_floor_scope(out, question)
        out.cost.wall_seconds = round(time.time() - t0, 2)
    return out


def _one_floor_scope(ans, question: str) -> None:
    """A whole-building question answered from ONE sheet's measured outline: that is one floor; said, capped at 0.35
    (T3's project scope sums the levels of a multi-file upload)."""
    from stoa.sheet.sets import _storey_of

    if not ans.trace or not str(ans.trace[0]).startswith("footprint (VP)") or not _WHOLE_Q.search(question) or \
            _storey_of(question)[0] is not None:
        return
    ans.confidence = min(float(ans.confidence), 0.35)
    ans.assumptions = list(ans.assumptions) + ["this sheet shows one floor: the value is that floor's gross area"]
    ans.missing = list(ans.missing) + ["the plans of the building's other floors (upload them together for the "
                                       "whole-building total)"]


def _measured_answer(path: str, question: str, page: int = 0):
    """(Answer from the drawn outline, footprint dict) or (None, None) when no outline is found."""
    from stoa.contract import Answer, Evidence

    view, several = None, None
    try:   # several plan views on the sheet: the one the question names ('second floor', 'module 02')
        from stoa.sheet.views import pick_views, views

        vs = views(path, page)
        plans = [v for v in vs if v.kind in ("floor_plan", "drawing")]
        if len(plans) > 1:
            picked = [v for v in pick_views(question, vs).views if v.kind in ("floor_plan", "drawing")]
            if len(picked) == 1:
                view = picked[0]
            else:
                several = [v.title for v in (picked or plans)]
    except Exception:  # noqa: BLE001
        view = None
    r = footprint(path, page, view=view)
    if not r.get("gia_m2"):
        return None, None
    if several:
        r["assumptions"].append(f"{len(several)} plan views on this sheet ({'; '.join(several[:4])}): measured the "
                                f"largest ({r['view']}); name the plan to measure another")
        r["confidence"] = min(r["confidence"], 0.45)
    ext = bool(_EXTERNAL_Q.search(question))
    v_m2 = r["gea_m2"] if ext else r["gia_m2"]
    sqft = bool(_SQFT_Q.search(question))
    value, unit = (round(v_m2 * 10.7639, 0), "ft2") if sqft else (round(v_m2, 1), "m2")
    other = (f"gross internal (GIFA) {r['gia_m2']:,} m²" if ext else
             f"gross external (to the outer face of the walls) {r['gea_m2']:,} m²")
    ev = [Evidence(file=str(path), page=int(page), bbox=r["bbox"], bbox_space="pdf_pt", polygon=r["polygon"] or None,
                   exact=False, note=f"building outline: {'GEA' if ext else 'GIA'} {v_m2:,} m² at 1:{r['ratio']:g}")]
    for vd in r.get("voids", []):
        ev.append(Evidence(file=str(path), page=int(page), bbox=vd["bbox"], bbox_space="pdf_pt", exact=False,
                           note=f"void left out of the floor area ({vd['m2']:,} m²)"))
    assumptions = list(r["assumptions"]) + [f"also: {other}"]
    if not ext and r.get("wall_m"):
        assumptions.append(f"gross internal = to the inner face of the outer walls ({r['wall_m']} m thick); "
                           "internal walls, stairs and shafts included (RICS GIA)")
    ans = Answer(value=value, unit=unit, confidence=float(r["confidence"]), evidence=ev,
                 trace=[f"footprint (VP): {r['view']} at 1:{r['ratio']:g}; outline {r['gea_m2']:,} m² "
                        f"(parts {r['parts'][:4]}), outer walls {r['wall_m']} m, voids "
                        f"{[vd['m2'] for vd in r.get('voids', [])]} -> {'GEA' if ext else 'GIA'} {v_m2:,} m²"],
                 solver="footprint", assumptions=assumptions, missing=list(r["missing"]))
    if r.get("alt_gea_m2"):
        ans.clarify = {"ask": "Which outline is the building?", "why": r["missing"][0] if r["missing"] else "",
                       "options": [{"label": f"outline found: {r['gea_m2']:,} m²", "value": r["gea_m2"], "unit": "m2",
                                    "evidence": [0]},
                                   {"label": f"every double line as a wall: {r['alt_gea_m2']:,} m²",
                                    "value": r["alt_gea_m2"], "unit": "m2", "evidence": []}],
                       "click": None}
    return ans, r


_M2_Q = re.compile(r"\b(?:square\s+met(?:re|er)s?|sq\.?\s*m\b|sqm\b|m2\b|m²)", re.I)


def _with_printed(ans, r, rows, path, page, question, storey, space, max_conf: float = 0.85):
    """The measured answer `ans` (or None) combined with the sheet's printed gross rows: the printed row the question
    means becomes the answer (the measured outline its cross-check); otherwise `ans`, with the printed rows stated."""
    from stoa.contract import Answer, Evidence

    gia, gea = (r or {}).get("gia_m2"), (r or {}).get("gea_m2")
    row, why = pick_printed(rows, question, storey, (gia, gea))
    if row is None:
        if ans is not None and rows:
            seen = "; ".join(f"'{x['text']}'" for x in rows[:5])
            ans.assumptions = list(ans.assumptions) + [f"printed on the sheet: {seen} ({why}): not used"]
        return ans
    pv, pu = row["value"], row["unit"]
    p_m2 = pv if pu == "m2" else pv / 10.7639
    if _SQFT_Q.search(question) and pu == "m2":
        value, unit = round(pv * 10.7639, 0), "ft2"
    elif _M2_Q.search(question) and pu == "ft2":
        value, unit = round(p_m2, 1), "m2"
    else:
        value, unit = pv, pu
    same = (row["storey"] is not None and row["storey"] == storey) or why.startswith(("the printed row the measured",
                                                                                       "the sheet's only"))
    if not same:   # another floor / the whole building: this sheet's outline is no check of it
        r, gia, gea = None, None, None
    lab = row["label"].lower()
    basis = gea if re.search(r"\bgea\b|external|footprint", lab) else \
        gia if re.search(r"\bgifa\b|\bgia\b|internal", lab) else \
        min((x for x in (gia, gea) if x), key=lambda x: abs(x / p_m2 - 1), default=None)
    trace = [f"printed (VP): '{row['text']}' ({why})"]
    assumptions = [f"read from the sheet: '{row['text']}' ({why})"]
    if pu != unit:
        assumptions.append(f"converted from the printed {pv:,g} {'m²' if pu == 'm2' else 'sq ft'}")
    credible = bool(basis) and float((r or {}).get("confidence") or 0) >= 0.3 and 0.6 <= p_m2 / basis <= 1.6
    if basis and not credible:
        assumptions.append(f"not cross-checked: the outline measured ({basis:,.1f} m²) is not the whole building "
                           "(dense / thin-walled drawing)")
        basis = None
    if basis:
        d = (p_m2 / basis - 1) * 100
        agree = abs(d) <= 6
        # a picture's outline is read on a TRACED copy: a weaker check, its disagreement costs less
        conf = max_conf if agree else max_conf - (0.15 if space == "image_px" else 0.2)
        chk = (f"the drawn outline measures {basis:,.1f} m² ({d:+.1f} %: {'agrees' if agree else 'differs, check'})")
        trace.append(f"cross-check: {chk}")
        assumptions.append(chk)
    else:
        conf = max_conf - 0.15
        if not same:
            assumptions.append("not measured on the drawing: this sheet's plan is " +
                               ("one floor of the building" if row["kind"] == "total" else "another floor"))
        elif not any(a.startswith("not cross-checked") for a in assumptions):
            assumptions.append("not cross-checked: no building outline was measured on this sheet")
    parts = [x for x in rows if x["kind"] == ("storey" if row["kind"] == "total" else "part") and x["unit"] == pu
             and not x["net"]]
    parts = list({x["value"]: x for x in parts}.values()) if row["kind"] == "total" else parts
    if len(parts) >= 2 and abs(sum(x["value"] for x in parts) / pv - 1) <= 0.02:
        chk = (f"the sheet's own part totals add up: {' + '.join(format(x['value'], ',g') for x in parts[:6])}"
               f" = {sum(x['value'] for x in parts):,g} ({(sum(x['value'] for x in parts) / pv - 1) * 100:+.1f} %)")
        trace.append(f"cross-check: {chk}")
        assumptions.append(chk)
        conf = max(conf, max_conf - 0.05)
    ev = [Evidence(file=path, page=page, bbox=row["bbox"], bbox_space=space, exact=True,
                   note=f"printed: {row['text']}")]
    use = ans is not None and same and bool(basis)
    if use:
        ev += [e for e in ans.evidence[:1]]
    checked = bool(basis) or any(a.startswith("the sheet's own part totals") for a in assumptions)
    missing = [] if checked else ["no drawn outline or printed parts to cross-check the printed total"]
    if why == ONE_FLOOR_WHY:
        conf = min(conf, 0.35)
        assumptions.append("this sheet shows one floor: the value is that floor's printed gross area")
        missing.append("the plans (or a printed building total) of the building's other floors: upload them together "
                       "for the whole-building total")
    out = Answer(value=value, unit=unit, confidence=round(conf, 2), evidence=ev, solver="footprint",
                 trace=trace + (list(ans.trace) if use else []), assumptions=assumptions, missing=missing)
    return out


def _cached_trace(img: str):
    """P1's traced vector copy of a picture (<stem>.vec6.pdf in the upload cache) when it ALREADY exists and is fresh,
    + the factor pdf_pt -> image px and how the paper was found; None otherwise (never traces: ~60 s, never a cold OCR)."""
    try:
        from stoa.formats import vectorize as V
        from stoa.formats.normalize import _cache_dir
        from pathlib import Path

        hit, how = V.paper(img), "from its DPI metadata"
        if hit is None:
            from stoa.formats.paper import paper_of_image
            from stoa.sheet.sets import _cached_layer

            lay = _cached_layer(img)
            if lay is None:
                return None
            hit = paper_of_image(img, [i.text for i in lay.items])
            how = "from its pixel size and the sheet size it prints"
        if hit is None:
            return None
        dpi, sheet = hit
        src = Path(img)
        out = Path(_cache_dir(src)) / f"{src.stem}.vec{V.VERSION}.pdf"
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime or not V._traced_at(out, src, dpi):
            return None
        return str(out), dpi / 72.0, f"{sheet} at {dpi:.0f} dpi {how}"
    except Exception:  # noqa: BLE001
        return None


def _image_answer(img: str, question: str):
    """A picture of a CAD sheet: printed gross rows from its CACHED text layer (OCR: confidence <= 0.75) and / or
    the gross area measured on its traced vector copy (evidence mapped to image px, confidence <= 0.5)."""
    import time

    from stoa.contract import Evidence

    t0 = time.time()
    a, r = None, None
    tr = _cached_trace(img)
    if tr is not None:
        pdf, f, how = tr
        a, r = _measured_answer(pdf, question, 0)
        if a is not None:
            ev = []
            for e in a.evidence:
                ev.append(Evidence(file=img, page=None, bbox=[round(v * f, 1) for v in e.bbox] if e.bbox else None,
                                   bbox_space="image_px" if e.bbox else None, exact=False, note=e.note,
                                   polygon=[[round(x * f, 1), round(y * f, 1)] for x, y in e.polygon]
                                   if e.polygon else None))
            a.evidence = ev
            a.confidence = min(a.confidence, 0.5)
            a.assumptions = list(a.assumptions) + [f"read on the picture's traced vector copy ({how})"]
            a.clarify = None
    rows, storey = [], None
    try:
        from stoa.sheet.sets import _cached_layer

        lay = _cached_layer(img)
        if lay is not None:
            rows = printed_gross(img, 0, layer=lay)
            storey = _sheet_storey(img, 0, layer=lay) if rows else None
    except Exception:  # noqa: BLE001
        rows = []
    try:
        out = _with_printed(a, r, rows, img, None, question, storey, "image_px", max_conf=0.75)
    except Exception:  # noqa: BLE001
        out = a
    if out is not None:
        _one_floor_scope(out, question)
        out.cost.wall_seconds = round(time.time() - t0, 2)
    return out
