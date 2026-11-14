"""Raster line drawing -> vector PDF with an invisible text layer, so the VECTOR readers can read a picture of a CAD
sheet (P1, X1 wild set 15:13: the PNG renders of the line-only LEVEL_11 and German 1:100 sheets scored 0/4 and 2/7
while their PDFs are answered by E1's lineplan: room areas, room sides, label counts).

Only for images whose DPI metadata gives a standard paper size (E1 15:13: clean renders / exports carry it, e.g.
2807x1985 px @120 dpi = A2, 1653x2337 @200 dpi = A4; screenshots carry 72/96/144 dpi and photos nothing): then the
page in pt is exactly px * 72 / dpi, and the sheet's printed 1:N is the scale.

    hit = paper(png)                      -> (dpi, "A2") or None
    pdf = to_vector_pdf(png, out_pdf)     -> the vector copy (same page geometry as the paper sheet)

What is drawn:
- ink (dark < 120, light < 200 grey) traced as the outlines of its connected regions (cv2.findContours), one stroked
  path per outline, 1 px wide, black or grey: E1's linework rasterises them back to the same walls (its 'dark' / 'all'
  line sets);
- door swings found by O1's raster door-arc filter (stoa/solvers/raster/doorarcs.py, radius at the printed scale)
  erased from the ink and drawn as Bezier quarter arcs, so linework replaces them by their two radii (the doorway is
  closed at the wall line and the swing sector stays in the room) exactly as on a vector sheet;
- every OCR text item (stoa/solvers/raster/textlayer.text_layer: the image's text layer, cached per image) as
  invisible text (render mode 3) in its box, and the glyph ink inside those boxes removed (labels are not walls).
Evidence on the PDF (pdf_pt) maps back to the image linearly: px = pt * dpi / 72.
"""

from __future__ import annotations

from pathlib import Path

PAPERS_MM = {
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420), "A4": (210, 297), "A5": (148, 210),
    "Letter": (215.9, 279.4), "Legal": (215.9, 355.6), "Tabloid": (279.4, 431.8), "ANSI C": (431.8, 558.8),
    "ANSI D": (558.8, 863.6), "ANSI E": (863.6, 1117.6), "ARCH A": (228.6, 304.8), "ARCH B": (304.8, 457.2),
    "ARCH C": (457.2, 609.6), "ARCH D": (609.6, 914.4), "ARCH E": (914.4, 1219.2), "ARCH E1": (762.0, 1066.8),
}
PAPER_TOL = 0.02
DARK, LIGHT = 120, 200  # grey levels: dark ink / light ink (anti-aliasing and hatches below LIGHT)
ARC_THR_THIN = 0.4
VERSION = 6


def paper(path: str) -> tuple[float, str] | None:
    """(dpi, paper name) when the image's DPI metadata makes it a standard sheet within 2 % on both sides."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi")
            w, h = im.size
    except Exception:  # noqa: BLE001
        return None
    if not dpi:
        return None
    try:
        dx, dy = float(dpi[0]), float(dpi[1])
    except (TypeError, ValueError, IndexError):
        return None
    if dx < 50 or abs(dx - dy) > 0.01 * dx:
        return None
    a, b = sorted((w / dx * 25.4, h / dy * 25.4))
    for name, (p, q) in PAPERS_MM.items():
        if abs(a - p) <= PAPER_TOL * p and abs(b - q) <= PAPER_TOL * q:
            return dx, name
    return None


def _scale_n(items) -> float | None:
    from stoa.solvers.raster.textlayer import parse_scales

    ns = []
    for it in items:
        for s in parse_scales(it.text):
            n = s.get("n") or s.get("scale") or s.get("ratio")
            if n:
                ns.append(float(n))
    if not ns:
        return None
    from collections import Counter

    return Counter(ns).most_common(1)[0][0]


def _quarter(c, r: float, q: int, k: float):
    """Bezier control points (px) of the quarter arc of quadrant q (image angles q*90..q*90+90, y down)."""
    import math

    a0, a1 = math.radians(90 * q), math.radians(90 * q + 90)
    cx, cy = c
    p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
    p3 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
    kk = 0.5523 * r
    c1 = (p0[0] - kk * math.sin(a0), p0[1] + kk * math.cos(a0))
    c2 = (p3[0] + kk * math.sin(a1), p3[1] - kk * math.cos(a1))
    return [(x * k, y * k) for x, y in (p0, c1, c2, p3)]


def to_vector_pdf(png: str, out: str | None = None, dpi: float | None = None, text: bool = True) -> dict:
    """Write the vector copy; returns {"pdf", "dpi", "paper", "scale_n", "paths", "arcs", "words"}."""
    import cv2
    import numpy as np
    import pymupdf
    from PIL import Image

    hit = paper(png)
    if dpi is None:
        if hit is None:
            raise ValueError("no paper-size DPI metadata")
        dpi = hit[0]
    k = 72.0 / dpi  # pt per px
    with Image.open(png) as im:
        g = np.asarray(im.convert("L"))
    H, W = g.shape
    dark = (g < DARK).astype(np.uint8)
    # light = grey lines of their own, not the anti-aliasing halo around dark ones (a halo traced as a second line
    # moves every wall face ~2 px into the room: LEVEL_11 comms room 16.0 vs 16.68 m2 on the PDF)
    halo = cv2.dilate(dark, np.ones((5, 5), np.uint8))
    light = ((g >= DARK) & (g < LIGHT) & (halo == 0)).astype(np.uint8)

    items = []
    if text:
        from stoa.solvers.raster.textlayer import text_layer

        try:
            items = list(text_layer(png).items)
        except Exception:  # noqa: BLE001 — no text layer: lines only
            items = []
    scale_n = _scale_n(items)

    # glyph ink inside the text boxes is not linework: drop every ink region that lies entirely inside one box
    if items:
        tm = np.zeros_like(dark)
        for it in items:
            x0, y0, x1, y1 = (int(round(v)) for v in it.box)
            tm[max(0, y0 - 2):min(H, y1 + 3), max(0, x0 - 2):min(W, x1 + 3)] = 1
        for m in (dark, light):
            n, lab, st, _ = cv2.connectedComponentsWithStats(m | (dark if m is light else 0), connectivity=8)
            inside = np.bincount(lab[tm > 0].ravel(), minlength=n)
            full = (inside == st[:, cv2.CC_STAT_AREA])
            full[0] = False
            m[full[lab] & (m > 0)] = 0

    arcs = []
    if scale_n:
        from stoa.solvers.raster import doorarcs

        px_per_m = dpi / 0.0254 / scale_n
        # one pass at the thin-line threshold (thin 1-2 px CAD arcs score lower than CubiCasa's: German A4 @200 dpi
        # has 0 arcs at 0.55, 12 at 0.4 with radii 0.69-1.02 m); the default threshold's arcs when there are >= 3.
        # Full resolution: searching at ~60 px/m (4 s instead of 8 s on the German A4) lost 2 of its 12 arcs and the
        # top-right room went 10.58 -> 9.96 m2 (gold 10.49)
        try:
            dets = doorarcs.detect(png, px_per_m, thr=ARC_THR_THIN)
            strong = [d for d in dets if d["score"] >= doorarcs.THR]
            arcs = strong if len(strong) >= 3 else dets
            # detect() keeps ONE quadrant per hinge (distance NMS), so the second swing of a hinge with two (a door
            # into a room next to another door's swing; LEVEL_11 comms room: its own swing was lost and the room
            # 0.68 m2 short) is re-scored here on the other quadrants
            ink = (g < doorarcs.INK).astype(np.float32)
            more = []
            for a in arcs:  # the second swing's centre can sit a few px off the first's (a 1 px ring misses it)
                x, y, r = int(round(a["c"][0])), int(round(a["c"][1])), float(a["r_px"])
                for q2 in range(4):
                    if q2 == a["q"]:
                        continue
                    sc, bx, by = max((doorarcs._score_at(ink, x + dx, y + dy, r, q2), x + dx, y + dy)
                                     for dx in range(-3, 4) for dy in range(-3, 4))
                    if sc >= ARC_THR_THIN:
                        more.append({**a, "c": [float(bx), float(by)], "q": q2, "score": sc})
            arcs = arcs + more
        except Exception:  # noqa: BLE001
            arcs = []
        yy, xx = np.mgrid[0:H, 0:W]
        for a in arcs:
            cx, cy = a["c"]
            r = float(a["r_px"])
            q = int(a["q"])
            x0, x1 = int(max(0, cx - r - 4)), int(min(W, cx + r + 5))
            y0, y1 = int(max(0, cy - r - 4)), int(min(H, cy + r + 5))
            sx, sy = xx[y0:y1, x0:x1] - cx, yy[y0:y1, x0:x1] - cy
            d = np.hypot(sx, sy)
            ang = (np.degrees(np.arctan2(sy, sx)) - 90 * q) % 360
            ring = (np.abs(d - r) <= 2.5) & (ang >= 8) & (ang <= 82)
            for m in (dark, light):
                m[y0:y1, x0:x1][ring] = 0

    doc = pymupdf.open()
    page = doc.new_page(width=W * k, height=H * k)
    sh = page.new_shape()
    paths = 0
    for m, col in ((light, (0.6, 0.6, 0.6)), (dark, (0, 0, 0))):
        cs, _ = cv2.findContours(m, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in cs:
            if len(c) < 2:
                continue
            c = cv2.approxPolyDP(c, 0.7, True)
            pts = [(float(p[0][0]) * k, float(p[0][1]) * k) for p in c]
            if len(pts) < 2:
                continue
            sh.draw_polyline(pts + [pts[0]])
            sh.finish(color=col, width=k, closePath=False)
            paths += 1
    for a in arcs:
        p = _quarter(a["c"], float(a["r_px"]), int(a["q"]), k)
        sh.draw_bezier(*p)
        sh.finish(color=(0, 0, 0), width=k, closePath=False)
    sh.commit()
    words = 0
    for it in items:
        s = (it.text or "").strip()
        if not s:
            continue
        x0, y0, x1, y1 = (float(v) * k for v in it.box)
        bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
        lu = pymupdf.get_text_length(s, fontname="helv", fontsize=1.0) or 1.0
        fs = max(1.0, min(bh * 0.85, bw / lu))
        try:
            page.insert_text((x0, y0 + (bh + fs * 0.7) / 2), s, fontname="helv", fontsize=fs, render_mode=3)
            words += 1
        except Exception:  # noqa: BLE001 — a glyph the base font lacks: skip the item
            continue
    out = out or str(Path(png).with_suffix(f".vec{VERSION}.pdf"))
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(Path(out).parent))  # atomic: parallel workers / a failed save
    os.close(fd)                                                           # never leave a half-written cache file
    try:
        doc.save(tmp, garbage=3, deflate=True)
        os.replace(tmp, out)
    finally:
        doc.close()
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"pdf": out, "dpi": dpi, "paper": hit[1] if hit else None, "scale_n": scale_n, "paths": paths,
            "arcs": len(arcs), "words": words}
