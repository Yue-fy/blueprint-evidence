"""Circled one-letter TYPE tags on a raster plan (door / window types 'A'..'J' in small circles) and the sheet's own
schedule of those types ('A - 2'-2" x 9'-9" x 1½" GLASS').

D1 22:55 (HABS al0493, a hand-lettered 1940 WPA sheet): 'How many type A doors are on the second floor plan?' was a
VLM count (10 / 10 / 1 for A / B / G, 0 of 4 right). Tags here = (1) Hough circles at the tag size whose ring holds a
smaller letter with paper between them (a hand-lettered 'O' / 'C' / 'D' in a word is a ring with nothing inside), their
insides enlarged and read on ONE strip image by the OCR (as stoa/ocr/vfix.py), united with (2) the page reading's own
one-capital items that sit inside such a ring. Only letters the sheet's schedule lists are counted. On al0493 every
letter count equals D1's hand count (A 11, B 9, C 3, D 1, E 1, F 2, G 4, H 3, J 3 = 37).

    circle_tags(path, layer) -> [{"x", "y", "r", "letter", "box"}]   (cached per path in this process)
    schedule(layer) -> {letter: row text}
    STOA_O1_TYPE_TAGS=0 turns the reader off
"""

from __future__ import annotations

import os
import re

import numpy as np

from . import textlayer as T

ON = os.environ.get("STOA_O1_TYPE_TAGS", "1") != "0"
_SCHED_ROW = re.compile(r"^\s*([A-Z])\s*[-–—=:]\s*(\S.*)$")
_cache: dict[str, list[dict]] = {}


_LOOK_ROW = re.compile(r"^\s*[1lI|0]\s*[-–—=:]\s*(\S.*)$")  # 'J - 3'-0" x 6'-6"' read as '1-3'-0"×6'-6"'


def schedule(layer: T.TextLayer) -> dict[str, str]:
    """{letter: row} of a type schedule printed on the sheet: >= 3 lines 'X - <size or words>' with distinct capitals.
    A row of the same column whose letter the OCR read as a digit ('1-3'-0"') takes the next letter after the row above
    it (schedules skip I and O)."""
    rows: dict[str, tuple] = {}
    for i in layer.items:
        m = _SCHED_ROW.match(i.text)
        if m and re.search(r"\d|[A-Z]{3,}", m.group(2)) and m.group(1) not in rows:
            rows[m.group(1)] = (i.text.strip(), i)
    if len(rows) < 3:
        return {}
    x0 = float(np.median([it.box[0] for _, it in rows.values()]))
    lh = float(np.median([it.box[3] - it.box[1] for _, it in rows.values()]))
    for i in sorted(layer.items, key=lambda i: i.cy):
        if not _LOOK_ROW.match(i.text) or abs(i.box[0] - x0) > 1.0 * lh:
            continue
        above = [(k, it) for k, (_, it) in rows.items() if 0 < i.cy - it.cy <= 1.6 * lh]
        if len(above) != 1:
            continue
        nxt = chr(ord(above[0][0]) + 1)
        while nxt in "IO":
            nxt = chr(ord(nxt) + 1)
        if nxt.isalpha() and nxt <= "Z" and nxt not in rows:
            rows[nxt] = (f"{nxt}{i.text.strip()[1:]}", i)
    return {k: v[0] for k, v in rows.items()}


def _ring(dark: np.ndarray, x: float, y: float, r: float) -> float:
    H, W = dark.shape
    hit = 0
    for a in np.linspace(0, 2 * np.pi, 36, endpoint=False):
        for dr in (-1.5, -0.5, 0.5, 1.5):
            xx, yy = int(round(x + (r + dr) * np.cos(a))), int(round(y + (r + dr) * np.sin(a)))
            if 0 <= xx < W and 0 <= yy < H and dark[yy, xx]:
                hit += 1
                break
    return hit / 36


def circle_tags(path: str, layer: T.TextLayer, rmin: int = 10, rmax: int = 16) -> list[dict]:
    key = f"{path}{T.file_tag(path)}"
    if key in _cache:
        return _cache[key]
    import cv2
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    im = Image.open(T._abspath(path)).convert("RGB")
    g = np.asarray(im.convert("L"))
    dark = g < 140
    cs = cv2.HoughCircles(cv2.GaussianBlur(g, (3, 3), 0), cv2.HOUGH_GRADIENT, dp=1, minDist=2 * rmin, param1=120,
                          param2=16, minRadius=rmin, maxRadius=rmax)
    keep = []
    for x, y, r in ([] if cs is None else [tuple(map(float, c)) for c in cs[0]]):
        if _ring(dark, x, y, r) < 0.8:
            continue
        yy, xx = np.mgrid[-int(r) - 2:int(r) + 3, -int(r) - 2:int(r) + 3]
        rr = np.hypot(xx, yy)
        win = dark[max(0, int(y) - int(r) - 2):int(y) + int(r) + 3, max(0, int(x) - int(r) - 2):int(x) + int(r) + 3]
        if win.shape != rr.shape:
            continue
        if win[(rr >= 0.62 * r) & (rr <= 0.8 * r)].mean() > 0.3 or win[rr <= 0.45 * r].mean() < 0.08:
            continue
        keep.append((x, y, r))
    out: list[dict] = []
    med = float(np.median([k[2] for k in keep])) if keep else 0.5 * (rmin + rmax)
    keep = [k for k in keep if abs(k[2] - med) <= 0.2 * med]
    if keep:
        try:
            letters = _read_strip(path, im, keep)
        except Exception:  # noqa: BLE001 - the page reading's own letters still count below
            letters = {}
        for n, (x, y, r) in enumerate(keep):
            t = letters.get(n, "").strip().upper().replace("0", "O").replace("1", "I")
            if len(t) == 1 and t.isalpha():
                out.append({"x": x, "y": y, "r": r, "letter": t, "box": [x - r, y - r, x + r, y + r], "src": "circle"})
    for it in layer.items:  # the page reading's one-capital items inside a ring
        t = it.text.strip().upper()
        if len(t) != 1 or not t.isalpha():
            continue
        if any(abs(it.cx - o["x"]) < med and abs(it.cy - o["y"]) < med for o in out):
            continue
        best = max((_ring(dark, it.cx, it.cy, r), r) for r in np.arange(0.8 * med, 1.25 * med + 0.1, 0.5))
        if best[0] >= 0.8:
            r = best[1]
            out.append({"x": it.cx, "y": it.cy, "r": r, "letter": t, "box": [it.cx - r, it.cy - r, it.cx + r, it.cy + r],
                        "src": "reading"})
    _cache[key] = out
    return out


def _read_strip(path: str, im, keep: list) -> dict[int, str]:
    from PIL import Image

    from stoa import ocr

    crops = []
    for x, y, r in keep:
        k = int(0.78 * r)
        crops.append(im.crop((int(x) - k, int(y) - k, int(x) + k + 1, int(y) + k + 1)).resize(
            ((2 * k + 1) * 4, (2 * k + 1) * 4), Image.LANCZOS))
    groups, cur, y0 = [], [], 30
    for n, c in enumerate(crops):
        if cur and y0 + c.height + 40 > 3600:
            groups.append(cur)
            cur, y0 = [], 30
        cur.append((n, c, y0))
        y0 += c.height + 40
    if cur:
        groups.append(cur)
    key = ocr._key(path)
    got: dict[int, list] = {}
    for s_i, group in enumerate(groups):
        s = Image.new("RGB", (max(c.width for _, c, _ in group) + 80, max(y + c.height for _, c, y in group) + 30),
                      "white")
        for n, c, y in group:
            s.paste(c, (40, y))
        sp = key.with_name(f"{key.stem}.vstrip-tags{s_i}.png")  # '.vstrip' in the name: no vfix on it
        from stoa.ocr.vfix import _save_atomic

        _save_atomic(s, sp)
        for it in ocr.read(str(sp)).get("items") or []:
            cy = (it["box"][1] + it["box"][3]) / 2
            for n, c, y in group:
                if y - 5 <= cy <= y + c.height + 5:
                    got.setdefault(n, []).append(it["text"])
    return {n: "".join(v) for n, v in got.items()}
