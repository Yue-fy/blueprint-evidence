"""PP-OCRv6 (PaddleOCR 3.x, Apache-2.0) word/line boxes for raster drawings. Runs in its OWN env.

This file imports only the standard library, numpy, PIL and paddleocr, so it runs in `$STOA_OCR_PYTHON`
(default .venv-ocr/bin/python). PaddleOCR pins numpy 2.3 and opencv-contrib 4.10;
installing it into `$JUNCTION_VENV` would replace the shared env's numpy 2.5 and cv2 5.0 under every running
agent and the captain's demo, so it lives in a separate env and the main code talks to it through JSON
(`stoa/ocr/__init__.py`).

    python stoa/ocr/engine.py IMG [IMG ...] [--out-dir DIR]     # batch: one JSON per image (indexing)
    python stoa/ocr/engine.py --serve 8110                      # HTTP: POST {"path": IMG} -> JSON

Output per image: {"size": [W, H], "items": [{"text", "box": [x0, y0, x1, y1], "score", "rot"}], "seconds",
"engine"}. Boxes are ORIGINAL image pixels. `rot` is 0 for normal lines and 90/270 for vertical lines (title
blocks on HABS sheets run up the right margin) that were re-read on a rotated crop.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

os.environ.setdefault("PADDLE_PDX_CACHE_HOME", ".stoa/paddlex")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

SIZE = os.environ.get("STOA_OCR_SIZE", "small")  # PP-OCRv6 tiny | small | medium
THREADS = int(os.environ.get("STOA_OCR_THREADS", "8"))
MAX_SIDE = 4000  # det input cap; bigger images are tiled at this size
TILE_OVERLAP = 200
VERTICAL = 2.5  # a box this many times taller than wide is a vertical line
UPSCALE = float(os.environ.get("STOA_OCR_UPSCALE", "0") or 0)  # >1: enlarge images below MAX_SIDE first (small print)
ENGINE = f"PP-OCRv6_{SIZE}+rot" + (f"+up{UPSCALE:g}" if UPSCALE > 1 else "")
_lock = threading.Lock()
_ocr = None


def _model():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR

        # enable_mkldnn=False: paddle 3.3.1's oneDNN path raises 'ConvertPirAttribute2RuntimeAttribute not support'
        # on the v6 det model (CPU)
        _ocr = PaddleOCR(text_detection_model_name=f"PP-OCRv6_{SIZE}_det",
                         text_recognition_model_name=f"PP-OCRv6_{SIZE}_rec",
                         use_doc_orientation_classify=False, use_doc_unwarping=False,
                         use_textline_orientation=False, device="cpu", cpu_threads=THREADS, enable_mkldnn=False)
    return _ocr


def _predict(arr: np.ndarray, side: int) -> list[dict]:
    res = _model().predict(arr, text_det_limit_type="max", text_det_limit_side_len=side)
    out = []
    for r in res:
        j = r.json.get("res", r.json)
        for t, s, b in zip(j["rec_texts"], j["rec_scores"], j["rec_boxes"]):
            if t.strip():
                out.append({"text": t.strip(), "box": [float(v) for v in b], "score": round(float(s), 3), "rot": 0})
    return out


def _tiles(w: int, h: int) -> list[tuple[int, int, int, int]]:
    if max(w, h) <= MAX_SIDE:
        return [(0, 0, w, h)]
    step = MAX_SIDE - TILE_OVERLAP
    xs = list(range(0, max(1, w - TILE_OVERLAP), step))
    ys = list(range(0, max(1, h - TILE_OVERLAP), step))
    return [(x, y, min(w, x + MAX_SIDE), min(h, y + MAX_SIDE)) for y in ys for x in xs]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return inter / small


def _dedupe(items: list[dict]) -> list[dict]:
    """Tile overlaps read some lines twice: keep the higher-scoring of two boxes that overlap >= 60%."""
    items = sorted(items, key=lambda i: -i["score"])
    kept: list[dict] = []
    for it in items:
        if all(_iou(it["box"], k["box"]) < 0.6 for k in kept):
            kept.append(it)
    return sorted(kept, key=lambda i: (round(i["box"][1] / 20), i["box"][0]))


_SCALEISH = re.compile(r"SCALE|[S58]C[A4][LI1][E3]|=|\d\s*/\s*\d|['’]\s*-", re.I)
_FIXES = [
    (re.compile(r"\b[S58]C[A4][LI1][E3]\b"), "SCALE"),  # '8CALE'
    (re.compile(r"(?<=['’`]-)\s*[Oo](?=\s*[\"”°])"), "0"),  # I'-O" -> I'-0"
    (re.compile(r"(?<=[\d/])\s*°"), '"'),  # 1/8° -> 1/8"
    (re.compile(r"(?<![\w])[IlfJ|](?=\s*['’`°\"]?\s*-\s*\d)"), "1"),  # I'-0" / f-0 -> 1'-0 / 1-0
    (re.compile(r"(?<=\d)\s*/\s*(?=\d)"), "/"),  # 3/ 8 -> 3/8
    (re.compile(r'(?<=\d)\s+"'), '"'),  # 8 " -> 8"
    (re.compile(r"(?<=\d)\s*['’`]\s*-\s*(?=\d)"), "'-"),  # 1' - 0 -> 1'-0
    (re.compile(r"(?<=\d)-\s*(?=\d+\s*[°\"])"), "'-"),  # 1-0" (foot mark lost) -> 1'-0"
    (re.compile(r"(?<![\w])[Il|](?=\s*[\"”]\s*=)"), "1"),  # I"=400' -> 1"=400'
    # '1"=400"': nobody writes 1 inch = 400 inches; the closing mark is a foot mark misread
    (re.compile(r"(?<=\b1[\"”]=)(\s*\d{3,}(?:,\d{3})*)\s*[\"”]"), r"\1'"),
    # map scales '1" = 12,000'' read with a foot mark: '1' = 12000' with a big bare number is an inch on paper
    (re.compile(r"\b1\s*['’`]\s*=\s*(?=(?:\d{1,3}(?:,\d{3})+|\d{4,})(?!\s*['’`\"\d]))"), '1"='),
]


_NUMISH = re.compile(r"^(?=(?:.*\d){2})[\dOo$Sl]+(?:[.,][\dOo$Sl]+)?$")
_DIGIT = str.maketrans({"O": "0", "o": "0", "$": "5", "S": "5", "l": "1"})
_OF = re.compile(r"\b(\d{1,3})\s*[°º0oO]\s*[Ff]\s*(\d{1,3})\b")
_ITOK = re.compile(r"(?<![\w.])(?=[Il]?\d)[Il]?\d[\d.,]*[Il]?(?![\w])")


def fix_text(t: str) -> str:
    """Undo PP-OCR's usual misreads in scale notes and ft-in dimensions ('SCALE: 1/4"=I'-0"', '8CALE 1/8° - f-0°'),
    and digit look-alikes in a purely numeric label ('1.1$' -> '1.15', room numbers / areas). Only lines that look
    like a number, a scale or a dimension are touched."""
    if _NUMISH.match(t):
        return t.translate(_DIGIT)
    t = _OF.sub(r"\1 OF \2", t)  # 'SHEET 9 °F 15' -> 'SHEET 9 OF 15'
    t = _ITOK.sub(lambda m: m.group(0).replace("I", "1").replace("l", "1"), t)  # 'KM I6.5' -> 'KM 16.5'
    if not _SCALEISH.search(t):
        return t
    for rx, rep in _FIXES:
        t = rx.sub(rep, t)
    return t


def _pass(img: Image.Image) -> list[dict]:
    W, H = img.size
    items: list[dict] = []
    for x0, y0, x1, y1 in _tiles(W, H):
        arr = np.asarray(img.crop((x0, y0, x1, y1)))
        for it in _predict(arr, MAX_SIDE):
            b = it["box"]
            it["box"] = [round(b[0] + x0, 1), round(b[1] + y0, 1), round(b[2] + x0, 1), round(b[3] + y0, 1)]
            items.append(it)
    return _dedupe(items)


def _unrotate(box: list[float], rot: int, W: int, H: int) -> list[float]:
    """Box in an image rotated by PIL rotate(rot, expand=True) (counter-clockwise) -> original-image box.
    rot 90: (x, y) -> (y, W - x), so x = W - y', y = x'. rot 270: (x, y) -> (H - y, x), so x = y', y = H - x'."""
    x0, y0, x1, y1 = box
    if rot == 90:
        xs, ys = (W - y0, W - y1), (x0, x1)
    else:
        xs, ys = (y0, y1), (H - x0, H - x1)
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _vertical(img: Image.Image, items: list[dict]) -> list[dict]:
    """Vertical lines (HABS title blocks run up the right margin; vertical dimension strings): the v6 recogniser reads
    only horizontal text, so they come back as noise ('HISIRC RICAN' for 'HISTORIC AMERICAN'). Read the whole page
    again rotated by 90 and 270 degrees, keep the lines that are vertical in the original frame (the better-scoring
    orientation wins), and drop the noisy upright readings they cover. Two extra passes (~2x CPU), 0 tokens."""
    W, H = img.size
    if not any((i["box"][3] - i["box"][1]) > VERTICAL * (i["box"][2] - i["box"][0]) for i in items):
        return items
    rotated: list[dict] = []
    for rot in (90, 270):
        for it in _pass(img.rotate(rot, expand=True)):
            b = _unrotate(it["box"], rot, W, H)
            if (b[3] - b[1]) > 1.5 * (b[2] - b[0]):  # vertical in the original = horizontal in the rotated pass
                rotated.append({**it, "box": b, "rot": rot})
    rotated = _dedupe(rotated)
    out = [i for i in items if not ((i["box"][3] - i["box"][1]) > 1.2 * (i["box"][2] - i["box"][0])
                                    and any(_iou(i["box"], r["box"]) >= 0.3 for r in rotated))]
    return _dedupe(out + rotated)


def read(path: str) -> dict:
    t0 = time.perf_counter()
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):  # transparent pixels -> white paper, not black (convert drops alpha)
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    W, H = img.size
    f = min(UPSCALE, MAX_SIDE / max(W, H)) if UPSCALE > 1 else 1.0
    if f > 1.05:
        img = img.resize((round(W * f), round(H * f)), Image.LANCZOS)
    with _lock:
        items = _vertical(img, _pass(img))
    if f > 1.05:
        for it in items:
            it["box"] = [round(v / f, 1) for v in it["box"]]
    for it in items:
        t = fix_text(it["text"])
        if t != it["text"]:
            it["raw"], it["text"] = it["text"], t
    return {"size": [W, H], "items": items, "seconds": round(time.perf_counter() - t0, 2), "engine": ENGINE}


def serve(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _model()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"ok": True, "engine": ENGINE})

        def do_POST(self):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                self._send(200, read(req["path"]))
            except Exception as e:  # noqa: BLE001 - report to the client, keep serving
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main(argv: list[str]) -> None:
    if argv[:1] == ["--serve"]:
        serve(int(argv[1]) if len(argv) > 1 else 8110)
        return
    out_dir = None
    if "--out-dir" in argv:
        i = argv.index("--out-dir")
        out_dir = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    for p in argv:
        try:
            d = read(p)
        except Exception as e:  # noqa: BLE001
            d = {"error": f"{type(e).__name__}: {e}"}
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, os.path.basename(p) + ".json"), "w") as f:
                json.dump(d, f)
            print(p, len(d.get("items", [])), d.get("seconds"), d.get("error", ""), flush=True)
        else:
            print(json.dumps(d), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
