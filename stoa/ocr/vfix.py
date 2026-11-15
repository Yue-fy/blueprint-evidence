"""Re-read short VERTICAL text lines (dimension strings up the side of a plan) the OCR read upside down.

The engine re-reads the whole page rotated by 90 / 270 degrees only when some line is > 2.5x taller than wide
(engine.VERTICAL: long HABS title-block lines). A short vertical dimension string ('3,07', '290', '4.50') is only
1.3-2.5x taller than wide, so it never triggers that pass, and the recogniser's own turn of a tall crop goes one way
only: a string printed bottom-to-top (the usual way on the left of a plan) comes back upside down as noise ("O'm"
@0.49 for '3,07' on the 1951 German plan, so the room's depth was missing and the kitchen area wrong).

Here, after a reading, the tall upright lines are cut out, each turned both ways, stacked into one small 'strip'
image, and that strip is read by the same OCR (server or subprocess: one small read, ~1-3 s, 0 tokens). A line takes
the better-scoring turn when it beats its upright reading. The result is kept in a sidecar '<key>.vfix.json' next to
the reading (the reading itself is never rewritten: other checkouts sharing the cache keep their behaviour).

    apply(path, d) -> d with fixed items (or d unchanged)        STOA_OCR_VFIX=0 turns it off
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ON = os.environ.get("STOA_OCR_VFIX", "1") != "0"
TALL = 1.3  # h > TALL * w: a candidate vertical line (engine.VERTICAL = 2.5 triggers the whole-page pass)
MIN_H = 18  # px
MAX_CAND = 40
MIN_NEW = 0.6  # a turned reading must score at least this ...
GAIN = 0.05  # ... and beat the upright one by this much
STRIP_MAX_H = 3600  # px; under the engine's 4000 px tile side
TEXT_H = 40  # px: small crops are enlarged to about this line height
TAG = "vf3"
_DIM_PUNCT = "'\"’”.,-/+:×()°²³"  # parts of a dimension string ('21'-9"', '3,07', '4.125')
_LINE_BITS = "-–—_|~一"  # the dimension line runs on past the text's ends: read as dashes around it


def _wh(b) -> tuple[float, float]:
    return b[2] - b[0], b[3] - b[1]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def candidates(items: list[dict]) -> list[int]:
    """Indices of upright lines that look vertical (taller than wide) and that no rotated read already covers."""
    turned = [i["box"] for i in items if i.get("rot")]

    def inside(a, b) -> float:  # share of box a inside box b
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        return ix * iy / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))

    out = []
    for k, it in enumerate(items):
        w, h = _wh(it["box"])
        if it.get("rot") or w < 6 or h < MIN_H or h <= TALL * w:
            continue
        if any(_iou(it["box"], b) >= 0.3 or inside(it["box"], b) >= 0.5 for b in turned):
            continue
        out.append(k)
    out.sort(key=lambda k: items[k].get("score", 1.0))  # the worst readings first
    return sorted(out[:MAX_CAND])


def strips(img, items: list[dict], idxs: list[int]) -> list[tuple[object, list[tuple]]]:
    """[(strip image, slots)]; a slot = (item index, rot, x0, y0, x1, y1) of one turned crop on the strip."""
    from PIL import Image

    W, H = img.size
    rows = []
    for k in idxs:
        x0, y0, x1, y1 = items[k]["box"]
        w, h = x1 - x0, y1 - y0
        px, py = max(3.0, 0.25 * w), max(3.0, 0.08 * h)
        c = img.crop((max(0, int(x0 - px)), max(0, int(y0 - py)), min(W, int(x1 + px + 1)), min(H, int(y1 + py + 1))))
        for rot in (90, 270):
            r = c.rotate(rot, expand=True)  # counter-clockwise, as the engine's whole-page pass (engine._unrotate)
            f = min(3.0, max(1.0, TEXT_H / max(1, r.height)))
            if r.width * f > 3000:
                f = max(1.0, 3000 / r.width)
            if f > 1.01:
                r = r.resize((max(1, round(r.width * f)), max(1, round(r.height * f))), Image.LANCZOS)
            rows.append((k, rot, r))
    out, cur, y = [], [], 30
    gap, margin = 50, 40
    for k, rot, r in rows:
        if cur and y + r.height + gap > STRIP_MAX_H:
            out.append(cur)
            cur, y = [], 30
        cur.append((k, rot, r, y))
        y += r.height + gap
    if cur:
        out.append(cur)
    res = []
    for group in out:
        Wd = max(r.width for _, _, r, _ in group) + 2 * margin
        Hd = max(y0 + r.height for _, _, r, y0 in group) + 30
        s = Image.new("RGB", (Wd, Hd), (255, 255, 255))
        slots = []
        for k, rot, r, y0 in group:
            s.paste(r, (margin, y0))
            slots.append((k, rot, margin, y0, margin + r.width, y0 + r.height))
        res.append((s, slots))
    return res


def _slot_text(reads: list[dict], slot: tuple) -> tuple[str, float]:
    _, _, sx0, sy0, sx1, sy1 = slot
    hit = []
    for it in reads:
        b = it["box"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if sx0 - 5 <= cx <= sx1 + 5 and sy0 - 5 <= cy <= sy1 + 5:
            hit.append(it)
    if not hit:
        return "", 0.0
    hit.sort(key=lambda it: it["box"][0])
    text, prev = "", None
    for it in hit:
        if prev is not None:
            h = max(1.0, it["box"][3] - it["box"][1])
            text += " " if it["box"][0] - prev["box"][2] > 0.5 * h else ""
        text += it["text"].strip()
        prev = it
    n = sum(max(1, len(it["text"])) for it in hit)
    score = sum(it.get("score", 0.0) * max(1, len(it["text"])) for it in hit) / n
    return text.strip().strip(_LINE_BITS).strip(), round(score, 3)


def _plausible(new: str, was: str, was_score: float) -> bool:
    """A turned reading worth taking: mostly letters / digits ('=≠=' / '0 一 一 0' are hatching), and one character
    only where the upright reading was below the engine's noise floor (an upright '1' is naturally tall: turned, it
    reads 'I')."""
    chars = [c for c in new if not c.isspace()]
    alnum = sum(1 for c in chars if c.isdigit() or (c.isalpha() and ord(c) < 0x250))  # '一' is 'alnum' too
    good = alnum + sum(1 for c in chars if c in _DIM_PUNCT)
    if not alnum or good < 0.75 * len(chars):
        return False
    from stoa.ocr import MIN_SCORE

    return len(chars) > 1 or was_score < MIN_SCORE


def merge(items: list[dict], slots_reads: list[tuple[list[tuple], list[dict]]]) -> tuple[list[dict], int]:
    """Items with each candidate replaced by its better turned reading. -> (items, number replaced)."""
    best: dict[int, tuple[str, float, int]] = {}
    for slots, reads in slots_reads:
        for slot in slots:
            text, score = _slot_text(reads, slot)
            k, rot = slot[0], slot[1]
            if text and (k not in best or score > best[k][1]):
                best[k] = (text, score, rot)
    out, n = [], 0
    for k, it in enumerate(items):
        if k in best:
            text, score, rot = best[k]
            old = it.get("score", 1.0)
            if score >= MIN_NEW and score >= old + GAIN and _plausible(text, it["text"], old):
                it = {**it, "text": text, "score": score, "rot": rot, "vfix": {"was": it["text"], "score": old}}
                n += 1
        out.append(it)
    return out, n


def _save_atomic(img, sp: Path) -> None:
    """Two processes may build the same strip at once: write a temp file and rename (a reader never sees half a PNG)."""
    tmp = sp.with_name(f"{sp.stem}.{os.getpid()}.tmp.png")
    img.save(tmp)
    os.replace(tmp, sp)


def apply(path: str, d: dict) -> dict:
    """The reading `d` of `path` with its short vertical lines re-read (cached in a sidecar); never raises."""
    if not ON or ".vstrip" in Path(path).name or not d.get("items") or d.get("vfix"):
        return d
    try:
        from stoa import ocr

        idxs = candidates(d["items"])
        if not idxs:
            return d
        key = ocr._key(path)
        side = key.with_suffix(".vfix.json")
        if side.exists() and ocr._cache_on():
            s = json.loads(side.read_text())
            if s.get("tag") == TAG and s.get("n_items") == len(d["items"]):
                return {**d, "items": s["items"], "vfix": s["n_fixed"],
                        "cpu_s": round(float(d.get("cpu_s") or 0) + float(s.get("cpu_s") or 0), 2) or d.get("cpu_s")}
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = None
        with Image.open(ocr._abspath(path)) as im:
            img = im.convert("RGB")
        if list(img.size) != list(d.get("size") or img.size):
            return d  # the reading is of another rendering of this file: boxes would not match
        reads, cpu = [], 0.0
        for n, (s_img, slots) in enumerate(strips(img, d["items"], idxs)):
            sp = key.with_name(f"{key.stem}.vstrip{n}.png")
            _save_atomic(s_img, sp)
            r = ocr.read(str(sp))
            cpu += float(r.get("cpu_s") or 0)
            reads.append((slots, r.get("items") or []))
        items, n_fixed = merge(d["items"], reads)
        try:
            ocr._write_atomic(side, {"tag": TAG, "path": str(path), "n_items": len(d["items"]), "items": items, "n_fixed": n_fixed,
                                     "candidates": len(idxs), "cpu_s": round(cpu, 2)})
        except OSError:
            pass
        return {**d, "items": items, "vfix": n_fixed,
                "cpu_s": round(float(d.get("cpu_s") or 0) + cpu, 2) or d.get("cpu_s")}
    except Exception as e:  # noqa: BLE001 - a better reading is a bonus, never a reason to lose the first one
        if os.environ.get("STOA_OCR_VFIX_DEBUG"):
            raise
        print(f"[ocr.vfix] {path}: {e}", file=__import__("sys").stderr)
        return d
