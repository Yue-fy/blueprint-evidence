"""F5 revision compare: two versions of the same sheet -> aligned -> what was added / removed / changed.

    $JUNCTION_VENV/bin/python -m stoa.features.revcompare A.pdf B.pdf [--page-a 0 --page-b 0] [--out DIR]

1. Load: a PDF page is rendered unrotated (the evidence contract's pdf_pt space) and its text words are read; an image
   (PNG / JPG, scans and photos) is read as grey pixels (image_px space).
2. Align B onto A (a 3x3 matrix, B render px -> A render px):
   - vector + vector: words printed once on both sheets ('Classroom 3', '7,200', the title block) give exact point
     pairs -> similarity transform (RANSAC). Text positions are geometry, so this is exact to ~0.1 pt.
   - otherwise (scans, photos, mixed): SIFT features on a downscaled copy -> similarity, or a homography when the
     sheet was photographed at an angle and it fits clearly better -> ECC refinement on blurred copies.
   - the result is checked by the share of A's linework that lands on B's linework (`overlap`); below 0.5 the
     sheets are reported as "not aligned" (different drawings, or a scan too poor to register) and no diff is made.
3. Diff: ink pixels of A farther than r px from any ink of B = removed, and the other way round = added (r grows
   with the alignment residual); changed pixels are grouped into regions (one region = one edit: a moved wall shows
   up as removed + added close together = 'changed'). Text words that differ between the sheets are paired by
   position ('62m2' -> '55m2'); printed numbers with a unit give a direct quantity delta (evidence = the two labels).
4. Impact on Stoa's question kinds is NOT computed here: `suggest_questions()` proposes questions (door / window
   counts when linework changed, areas of the rooms whose labels are next to a change) and the caller asks them on
   both files through the normal pipeline (/api/ask in the UI, `impact()` in-process for scripts), so answers keep
   the pipeline's evidence and confidence and no answer logic is forked.

Router: POST /feat/revcompare/compare {a, b, page_a, page_b} -> JSON; GET /feat/revcompare/image/{key}/{name}.png
(overlay = A in grey, removed red, added green; b_aligned = B warped onto A, for a swipe / flicker view). Feature flag:
STOA_FEATURES must list 'revcompare' (else 404), so the stable demo is untouched while the flag is off.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FEATURE = "revcompare"
RENDER_LONG = 3600  # px on the long side of the diff raster (A3 vector: ~3 px per pt)
SMALL_LONG = 1600  # px for feature matching / ECC
RASTER_R = int(os.environ.get("STOA_F5_R", "3"))  # pixel tolerance on scans / photos (+ alignment residual)
GHOST_PAPER = os.environ.get("STOA_F5_GHOST_PAPER", "p90")  # 'median' = cycle 1's paper level (eroded median)
SPECK = float(os.environ.get("STOA_F5_SPECK", "0.0035"))  # scans: a mark of only dots this small (x sheet side) = dust
GHOST = float(os.environ.get("STOA_F5_GHOST", "0.25"))  # share of a mark's contrast that still counts as "there" (scans)
CACHE_DIR = Path(os.environ.get("STOA_FEAT_CACHE", Path(tempfile.gettempdir()) / "stoa-feat")) / FEATURE
UPLOADS = Path(os.environ.get("STOA_UPLOADS", Path(tempfile.gettempdir()) / "stoa-uploads"))
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


# ----------------------------------------------------------------------------------------------------------- load
@dataclass
class Sheet:
    path: str
    kind: str  # "vector" (PDF with a text layer or line art) | "raster" (image, or image-only PDF)
    space: str  # evidence bbox_space of the native coordinates: "pdf_pt" | "image_px"
    gray: np.ndarray  # rendered page, uint8, white paper
    zoom: float  # render px per native unit
    native: tuple[float, float]  # page size in native units
    page: int | None = None
    words: list[tuple[float, float, float, float, str]] = field(default_factory=list)  # native units
    lines: list[tuple[float, float, float, float, str]] = field(default_factory=list)  # words joined per text line


def load_sheet(path: str | Path, page: int = 0, long_px: int = RENDER_LONG) -> Sheet:
    import cv2

    path = str(path)
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        import fitz

        with fitz.open(path) as doc:
            if not 0 <= page < len(doc):
                raise ValueError(f"{Path(path).name} has {_n(len(doc), 'page')}; page {page + 1} does not exist")
            pg = doc[page]
            pg.set_rotation(0)  # pdf_pt = the unrotated page (contract); alignment handles any rotation between sheets
            w, h = pg.rect.width, pg.rect.height
            zoom = long_px / max(w, h)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
            gray = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()
            raw = pg.get_text("words")
            ox, oy = pg.rect.x0, pg.rect.y0
            words = [(x0 - ox, y0 - oy, x1 - ox, y1 - oy, t) for x0, y0, x1, y1, t, *_ in raw if t.strip()]
            by_line: dict[tuple, list] = collections.defaultdict(list)
            for x0, y0, x1, y1, t, b, ln, _ in raw:
                if t.strip():
                    by_line[(b, ln)].append((x0 - ox, y0 - oy, x1 - ox, y1 - oy, t))
            lines = [(min(x[0] for x in v), min(x[1] for x in v), max(x[2] for x in v), max(x[3] for x in v),
                      " ".join(x[4] for x in v)) for v in by_line.values()]
            n_draw = len(pg.get_drawings()) if not words else 1
        kind = "vector" if (len(words) >= 8 or n_draw > 50) else "raster"
        return Sheet(path, kind, "pdf_pt", gray, zoom, (w, h), page, words, lines)
    if ext in IMAGE_EXT:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:  # PIL reads a few files OpenCV does not (16-bit PNG, CMYK JPEG)
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None
            img = np.asarray(Image.open(path).convert("L"))
        h, w = img.shape
        zoom = min(1.0, long_px / max(w, h))
        gray = cv2.resize(img, (round(w * zoom), round(h * zoom)), interpolation=cv2.INTER_AREA) if zoom < 1 else img
        return Sheet(path, "raster", "image_px", np.ascontiguousarray(gray), zoom, (float(w), float(h)), None)
    raise ValueError(f"revision compare reads PDF and image files, not {ext or 'files without a type'}")


def best_page(a: Sheet, path_b: str) -> tuple[int, str | None]:
    """For a multi-page PDF B: the page whose printed words overlap A's most (title, sheet number, room names)."""
    if Path(path_b).suffix.lower() != ".pdf":
        return 0, None
    import fitz

    with fitz.open(path_b) as doc:
        n_pages = len(doc)
        if n_pages <= 1:
            return 0, None
        mine = {_norm_tok(w[4]) for w in a.words if len(_norm_tok(w[4])) >= 3}
        if not mine:
            return 0, f"{n_pages} pages: compared with page 1 (sheet A has no text to match pages by)"
        scores = []
        for k in range(min(len(doc), 300)):
            ws = {_norm_tok(w[4]) for w in doc[k].get_text("words") if len(_norm_tok(w[4])) >= 3}
            scores.append(len(mine & ws) / max(1, len(mine | ws)))
    k = int(np.argmax(scores))
    return k, f"{n_pages} pages: page {k + 1} matches the sheet on screen best ({scores[k]:.0%} shared words)"


# ------------------------------------------------------------------------------------------------ sets of sheets
SET_MAX = int(os.environ.get("STOA_F5_SET_MAX", "40"))  # sheets read per side ('what changed in this issue')
_PLAIN_DEC = re.compile(r"^\d{1,2}\.\d{2,3}$")
_SHEET_WORD = re.compile(r"^(?:sheet|dwg|drawing|drg|no|nr|number|blatt|piirustus|ritning)[.:]?$", re.I)
_SHEET_NO = re.compile(r"^(?:[A-Z]{1,3}[-.]?\d{2,4}(?:\.\d{1,3})?[A-Z]?|[A-Z]{1,3}[-.]\d{1,4}[A-Z]?|\d{1,2}(?:\.\d{2,3}){1,3})$")
_BOILER = re.compile(r"\b(agency|ltd|limited|oy|oyj|ab|gmbh|inc|architects?|arkkitehd\w*|consult\w*|engineers?|designs?|"
                     r"company|client|project|drawn|checked|approved|scale|date|revision|rev|copyright|curriculum)\b|:$", re.I)
_TITLE_HINT = re.compile(r"\b(plan|floor|level|storey|elevation|section|details?|schedule|roof|site|ground|first|second|"
                         r"third|basement|layout|mezzanine|kerros|pohja(?:piirustus)?|julkisivu|leikkaus|grundriss|"
                         r"geschoss|ansicht|schnitt)\b", re.I)
_SHEETS: "collections.OrderedDict[tuple, list[dict]]" = collections.OrderedDict()
_FLOOR_TOK = re.compile(r"^(?:gf|ff|sf|tf|lg|ug|b\d?|l\d{1,2}|\d(?:st|nd|rd|th)|ground|first|second|third|fourth|fifth|"
                        r"basement|lower|upper|mezzanine|roof|level|\d{1,2}|[ivx]{1,4}|kellari|katto|eg|og|ug)$", re.I)


def _title_bonus(ta: str, tb: str) -> float:
    """Same title: +0.15. Titles that differ in a floor word ('... School FF' vs '... School GF', 'First Floor' vs
    'Ground Floor'): another storey of the same building, which shares most of its words: -0.3."""
    a = {_norm_tok(t) for t in ta.split() if _norm_tok(t)}
    b = {_norm_tok(t) for t in tb.split() if _norm_tok(t)}
    if not a or not b:
        return 0.0
    if a == b:
        return 0.15
    return -0.3 if any(_FLOOR_TOK.match(t) for t in a ^ b) else 0.0


def _page_info(pg) -> dict:
    """What a person reads to tell sheets apart: the title (the biggest printed line, plan / floor / elevation words
    preferred), the sheet number in the title-block corner (A-101, S-100F) and the set of words used to pair sheets."""
    w, h = pg.rect.width, pg.rect.height
    ox, oy = pg.rect.x0, pg.rect.y0
    title, best = "", 0.0
    for blk in pg.get_text("dict").get("blocks", []):
        for ln in blk.get("lines", []):
            spans = [sp for sp in ln.get("spans", []) if sp.get("text", "").strip()]
            txt = " ".join(sp["text"].strip() for sp in spans)
            if not spans or not 3 <= len(txt) <= 60 or sum(c.isalpha() for c in txt) < 3:
                continue
            # the biggest line wins; floor / plan words count more, firm names and labels ('Scale:') less, and between
            # equal sizes the longer, more specific line ('3FE 630 Place JMI Primary School FF', not 'Education Funding Agency')
            sc = (max(sp.get("size", 0) for sp in spans) * (1.6 if _TITLE_HINT.search(txt) else 1.0)
                  * (0.5 if _BOILER.search(txt) else 1.0) + 0.01 * len(txt))
            if sc > best:
                title, best = txt, sc
    words = pg.get_text("words")
    # a revision code ('C01', 'P02': printed beside REV / REVISION) is not the sheet number: on Stoa's DfE sheets it
    # was, so every sheet of a set shared 'C01' (+0.3 to every pairing) and the panel named sheets 'C01 ...'
    revs = any(_REV_WORD.search(t) for *_, t, _b, _l, _n in words)
    corner = [(y1 - y0, t.strip()) for x0, y0, x1, y1, t, *_ in words if (x0 - ox) > 0.5 * w and (y0 - oy) > 0.55 * h]
    iso = [x for x in corner if _ISO_NO.match(x[1]) and re.search(r"\d", x[1])]  # 'SEN-DFE-XX-00-D-X-0201'
    # a plain decimal ('2.02', '4.85', '18.00') is a room number, a detail callout or a value far more often than a sheet
    # number (19 of 19 on the team's 232 sheet pages; a Swiss DXF sheet's '4.85'): only beside a 'Sheet' / 'Dwg' label
    labelled = any(_SHEET_WORD.match(x[1]) for x in corner)
    nos = iso or [x for x in corner if _SHEET_NO.match(x[1]) and not (revs and _REV_CODE.match(x[1]))
                  and (labelled or not _PLAIN_DEC.match(x[1]))]
    toks = {_norm_tok(t) for *_, t, _b, _l, _n in words if len(_norm_tok(t)) >= 3}
    return {"title": _nice_title(title), "sheet_no": max(nos)[1] if nos else None, "tokens": toks,
            "kind": "vector" if len(words) >= 8 else "raster"}


_REV_WORD = re.compile(r"^rev(?:ision|s)?\b", re.I)
_REV_CODE = re.compile(r"^(?:[PCAT]\d{2}(?:\.\d+)?|[A-Z])$")  # P01 preliminary, C01 contract ...; 'Rev C'
_ISO_NO = re.compile(r"^[A-Z0-9]{2,8}(?:-[A-Z0-9]{1,8}){4,8}$")  # ISO 19650 / BS 1192 drawing number


def _nice_title(t: str) -> str:
    """'FIRST FLOOR PLAN' -> 'First Floor Plan' (shouting titles read badly in a sentence); mixed case stays."""
    t = re.sub(r"\s+", " ", t or "").strip(" -–:·")
    if t.isupper() and sum(c.isalpha() for c in t) >= 4:
        t = " ".join(w if (len(w) <= 3 and any(c.isdigit() for c in w)) else w.capitalize() for w in t.split(" "))
    return t[:70]


def sheet_name(path: str | Path, page: int | None = 0, virtual: str | None = None) -> dict:
    """What a person calls this sheet (UX NEW-11: the panel spoke in file names): the drawing title printed on it, its
    sheet number and revision code from the title block. A picture is named from the upload's OCR text layer when the
    warm-up has read it (never a cold read here), else from its file name ('source': 'file')."""
    p = Path(path)
    pg = int(page or 0)
    out = {"title": None, "number": None, "revision": None, "source": "file",
           "file": Path(virtual or str(p)).name}
    try:
        if p.suffix.lower() == ".pdf":
            st = p.stat()
            with _LOCK:
                hit = _SHEETS.get((str(p), st.st_size, st.st_mtime_ns))
            if hit is not None and pg < len(hit):  # the set's sheets were listed already (POST /sets)
                info = hit[pg]
            else:  # only this page (list_sheets reads up to 40: 3 s on a 9-page set, more on a 156-page one)
                import fitz

                with fitz.open(str(p)) as doc:
                    info = _page_info(doc[pg]) if pg < len(doc) else None
            if info:
                out["title"], out["number"] = info["title"] or None, info["sheet_no"]
            from stoa.features.takeoff import sheet_rev  # F2's reader: the ISO number + its revision code

            num, rev = sheet_rev(str(p), pg)
            out["number"], out["revision"] = num or out["number"], rev
        elif p.suffix.lower() in IMAGE_EXT and OCR_AREAS:
            from stoa.solvers.raster.sheet import cached_layer

            layer = cached_layer(str(p))
            if layer is not None:
                from stoa.sheet.sets import classify  # VP's sheet reader (title block / view titles) on that layer

                sh = classify(str(p), 0, layer)
                out["title"], out["number"] = sh.title, sh.number
    except Exception:  # noqa: BLE001 - a name is a nicety: never fail a compare over it
        pass
    if out["title"]:
        out["title"], out["source"] = _nice_title(out["title"]), "sheet"
    else:  # 'school-FF-rev-C.pdf' -> 'school FF rev C'
        out["title"] = re.sub(r"[_\s]+", " ", Path(out["file"]).stem).strip() or out["file"]
    return out


def _n(k: int, word: str, many: str | None = None) -> str:
    """'1 label', '3 labels', '2 door sets' (UX NEW-11: no 'label(s)')."""
    return f"{k} {word if k == 1 else (many or word + 's')}"


def list_sheets(path: str | Path, virtual: str | None = None) -> list[dict]:
    """Every sheet in one file: each page of a PDF, or the image itself. Cached by (path, size, mtime)."""
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    with _LOCK:
        if key in _SHEETS:
            return [{**x, "file": virtual or str(p)} for x in _SHEETS[key]]
    out: list[dict] = []
    if p.suffix.lower() == ".pdf":
        import fitz

        with fitz.open(str(p)) as doc:
            for k in range(min(len(doc), SET_MAX)):
                out.append({"page": k, "n_pages": len(doc), **_page_info(doc[k])})
    elif p.suffix.lower() in IMAGE_EXT:
        out.append({"page": 0, "n_pages": 1, "title": p.stem, "sheet_no": None, "tokens": set(), "kind": "raster"})
    else:
        raise ValueError(f"revision compare reads PDF and image files, not {p.suffix or 'files without a type'}")
    with _LOCK:
        _SHEETS[key] = out
        while len(_SHEETS) > 64:
            _SHEETS.popitem(last=False)
    return [{**x, "file": virtual or str(p)} for x in out]


def _thumb_features(path: str, page: int) -> tuple[np.ndarray, np.ndarray]:
    """SIFT on a ~1000 px render: pairs scans / photos (no text layer) with their sheet."""
    import cv2

    s = load_sheet(path, page, long_px=1000)
    sift = cv2.SIFT_create(nfeatures=2500)
    kp, des = sift.detectAndCompute(s.gray, None)
    pts = np.float32([k.pt for k in kp]).reshape(-1, 2) / max(s.gray.shape)  # share of the long side: any render size
    return pts, (des if des is not None else np.zeros((0, 128), np.float32))


def _feature_score(fa: tuple, fb: tuple) -> float:
    """n / (n + 150), n = features one similarity transform explains: the same sheet 0.6-0.9, another building < 0.05.
    Another FLOOR of the same building also scores high on a raster (shared footprint, grid and title block: 0.7 vs
    0.89 for the same floor), so pairs are taken best first and the full compare's same-sheet test has the last word."""
    import cv2

    (pa, da), (pb, db) = fa, fb
    if len(da) < 20 or len(db) < 20:
        return 0.0
    m = cv2.BFMatcher(cv2.NORM_L2).knnMatch(db, da, k=2)
    good = [x[0] for x in m if len(x) == 2 and x[0].distance < 0.75 * x[1].distance]
    if len(good) < 12:
        return 0.0
    src = np.float32([pb[g.queryIdx] for g in good])
    dst = np.float32([pa[g.trainIdx] for g in good])
    cv2.setRNGSeed(0)
    M, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=0.006)
    n = int(inl.sum()) if inl is not None else 0
    return n / (n + 150.0)


def match_sets(a: list[tuple[str, str]], b: list[tuple[str, str]]) -> dict:
    """Pair every sheet of issue A with its sheet in issue B ('what changed in this issue'): printed words when both
    sheets have a text layer (Jaccard of their words, +0.3 for the same sheet number), image features otherwise; the
    best pairs are taken first, one partner per sheet. Unpaired sheets are new in B / gone from A.
    a, b: [(absolute path, path shown to the visitor)]."""
    t0 = time.perf_counter()
    sa = [x for ap, vp in a for x in list_sheets(ap, vp)]
    sb = [x for bp, vp in b for x in list_sheets(bp, vp)]
    absa = {vp: ap for ap, vp in a}
    absa.update({vp: bp for bp, vp in b})
    feats: dict[tuple, tuple] = {}

    def feat(x: dict) -> tuple:
        k = (x["file"], x["page"])
        if k not in feats:
            feats[k] = _thumb_features(absa[x["file"]], x["page"])
        return feats[k]

    cands = []
    for i, x in enumerate(sa):
        for j, y in enumerate(sb):
            if x["kind"] == "vector" and y["kind"] == "vector" and x["tokens"] and y["tokens"]:
                sc = len(x["tokens"] & y["tokens"]) / max(1, len(x["tokens"] | y["tokens"]))
                if x["sheet_no"] and x["sheet_no"] == y["sheet_no"]:
                    sc += 0.3
                sc += _title_bonus(x["title"], y["title"])
                how, thr = "words", 0.35
            else:
                sc, how, thr = _feature_score(feat(x), feat(y)), "features", 0.17  # >= 30 matches
            if sc >= thr:
                cands.append((sc, i, j, how))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_a, used_b, pairs = set(), set(), []
    for sc, i, j, how in cands:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        # weak: words 0.35-0.65 (two designs that look alike, or a sheet redrawn); the compare's own same-sheet test decides
        pairs.append({"a": i, "b": j, "score": round(min(sc, 1.0), 3), "how": how,
                      "weak": bool(sc < (0.65 if how == "words" else 0.4))})
    pairs.sort(key=lambda q: (q["a"]))
    pub = lambda xs: [{k: v for k, v in x.items() if k != "tokens"} for x in xs]  # noqa: E731
    only_a = [i for i in range(len(sa)) if i not in used_a]
    only_b = [j for j in range(len(sb)) if j not in used_b]
    bits = [f"{_n(len(pairs), 'sheet')} in both"]
    if only_b:
        bits.append(f"{len(only_b)} only in the new issue")
    if only_a:
        bits.append(f"{len(only_a)} only in the old one")
    return {"sheets_a": pub(sa), "sheets_b": pub(sb), "pairs": pairs, "only_a": only_a, "only_b": only_b,
            "summary": ", ".join(bits), "seconds": round(time.perf_counter() - t0, 2),
            "cost": {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0, "api_calls": 0, "tier": "cheap"}}


def ink_mask(s: Sheet, level: str = "strong", exact_pair: bool = True, soften: float = 0.0) -> np.ndarray:
    """Dark pixels = linework and text. Two levels, so an edit must be STRONG ink on one sheet and not even WEAK ink on
    the other (a faint dashed line that a scan lost is not 'removed'). Vector renders compared with vector renders:
    one global threshold (both levels equal: the renders are exact). Otherwise (scans, photos, or a vector render
    against a scan) a local threshold on both (uneven paper, shadows): strong = 18 grey levels under the local mean,
    weak = max(8, 3 x the paper's noise) after more blur, specks removed."""
    import cv2

    g = s.gray
    if s.kind == "vector" and exact_pair:
        return g < 200
    # a vector render against a scan is thresholded like the scan (same local rule both sides) after a blur like a
    # print + scan's: a faint line the scan kept is found on both, one it lost is weak on the render, not 'removed'
    if s.kind == "vector":
        g = cv2.GaussianBlur(g, (0, 0), 1.3)
    if soften > 0:  # this sheet is sharper than the other one (finer pixels): blur it down to the other's detail
        g = cv2.GaussianBlur(g, (0, 0), soften)
    if level == "strong":
        blur = cv2.GaussianBlur(g, (0, 0), 1.0)
        return cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 41, 18) > 0
    # weak: faint marks, but not scan noise: blur more, an offset above the noise, specks removed
    blur = cv2.GaussianBlur(g, (0, 0), 1.6)
    c = max(8.0, 3.0 * _noise(g))
    m = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 41, c)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) > 0


def _noise(g: np.ndarray) -> float:
    """Grey-level noise of the paper after the weak mask's blur (robust: median absolute deviation)."""
    import cv2

    hi = g.astype(np.float32) - cv2.GaussianBlur(g, (0, 0), 3.0).astype(np.float32)
    lo = cv2.GaussianBlur(hi, (0, 0), 1.6)
    return float(1.4826 * np.median(np.abs(lo - np.median(lo))))


# ------------------------------------------------------------------------------------------------------- align
@dataclass
class Alignment:
    M: np.ndarray  # 3x3, B render px -> A render px
    method: str
    inliers: int
    residual_px: float  # median reprojection error of the inliers, in A render px
    overlap: float = 0.0  # share of linework that lands on the other sheet's linework (min of both ways)
    ok: bool = False
    note: str = ""

    def describe(self, a: Sheet) -> dict:
        lin = self.M[:2, :2]
        scale = math.sqrt(abs(np.linalg.det(lin)))
        angle = math.degrees(math.atan2(lin[1, 0], lin[0, 0]))
        unit = "pt" if a.space == "pdf_pt" else "px"
        return {"method": self.method, "ok": self.ok, "inliers": self.inliers, "overlap": round(self.overlap, 3),
                "residual": round(self.residual_px / a.zoom, 2), "residual_unit": unit,
                "scale": round(scale, 4), "rotation_deg": round(angle, 2),
                "perspective": bool(abs(self.M[2, 0]) + abs(self.M[2, 1]) > 1e-9),
                "matrix": [[round(float(v), 6) for v in row] for row in self.M], "note": self.note,
                "how": self.plain(a)}

    def plain(self, a: Sheet) -> str:
        """The alignment in words a first-time visitor reads (QA 21:25: 'features+ecc', '×1.0577' is jargon)."""
        if self.method == "none":
            return "could not be lined up"
        if self.method.startswith("text"):
            r = self.residual_px / a.zoom  # in the sheet's own units (UX M11: '(exact to 0.0 pt)' was jargon)
            if a.space != "pdf_pt":
                return f"by the words printed on both sheets (they match to within {max(1, round(r))} px)"
            mm = r * 25.4 / 72
            return ("by the words printed on both sheets (they match exactly)" if mm < 0.05 else
                    f"by the words printed on both sheets (they match to within {mm:.1f} mm on the paper)")
        lin = self.M[:2, :2]  # (render sizes differ by file type, so a scale here would mean nothing to a visitor)
        angle = math.degrees(math.atan2(lin[1, 0], lin[0, 0]))
        bits = []
        if abs(angle) >= 0.1:
            bits.append(f"turned {abs(angle):.1f}°")
        if abs(self.M[2, 0]) + abs(self.M[2, 1]) > 1e-9:
            bits.append("photographed at an angle (straightened)")
        shift = f": the other version was {', '.join(bits)}" if bits else ""
        return f"by matching the drawing's shapes{shift}"


def _norm_tok(t: str) -> str:
    return re.sub(r"\s+", "", t).strip(".,;:()[]").lower()


def _text_pairs(a: Sheet, b: Sheet) -> tuple[np.ndarray, np.ndarray]:
    """Centres (render px) of words printed exactly once on each sheet, paired by their text."""
    def uniq(s: Sheet) -> dict[str, tuple[float, float]]:
        seen: dict[str, list] = collections.defaultdict(list)
        for x0, y0, x1, y1, t in s.words:
            k = _norm_tok(t)
            if len(k) >= 3 or (len(k) >= 2 and any(c.isdigit() for c in k)):
                seen[k].append(((x0 + x1) / 2 * s.zoom, (y0 + y1) / 2 * s.zoom))
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    ua, ub = uniq(a), uniq(b)
    keys = sorted(set(ua) & set(ub))
    return (np.float32([ub[k] for k in keys]).reshape(-1, 2), np.float32([ua[k] for k in keys]).reshape(-1, 2))


def _small(g: np.ndarray, long_px: int = SMALL_LONG) -> tuple[np.ndarray, float]:
    import cv2

    f = min(1.0, long_px / max(g.shape))
    return (cv2.resize(g, (round(g.shape[1] * f), round(g.shape[0] * f)), interpolation=cv2.INTER_AREA) if f < 1 else g), f


def _sift_pairs(a: Sheet, b: Sheet) -> tuple[np.ndarray, np.ndarray]:
    import cv2

    sa, fa = _small(a.gray)
    sb, fb = _small(b.gray)
    sift = cv2.SIFT_create(nfeatures=8000)
    ka, da = sift.detectAndCompute(sa, None)
    kb, db = sift.detectAndCompute(sb, None)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
    knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(db, da, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < 0.8 * m[1].distance]
    pb = np.float32([kb[m.queryIdx].pt for m in good]).reshape(-1, 2) / fb
    pa = np.float32([ka[m.trainIdx].pt for m in good]).reshape(-1, 2) / fa
    return pb, pa


def _fit(pb: np.ndarray, pa: np.ndarray, thr: float, homography: bool = False) -> tuple[np.ndarray | None, int, float]:
    import cv2

    if len(pb) < (8 if homography else 4):
        return None, 0, float("inf")
    if homography:
        H, inl = cv2.findHomography(pb, pa, cv2.RANSAC, thr, maxIters=5000, confidence=0.999)
        if H is None:
            return None, 0, float("inf")
        M = H / H[2, 2]
    else:
        A, inl = cv2.estimateAffinePartial2D(pb, pa, method=cv2.RANSAC, ransacReprojThreshold=thr, maxIters=5000,
                                             confidence=0.999, refineIters=20)
        if A is None:
            return None, 0, float("inf")
        M = np.vstack([A, [0, 0, 1]])
    mask = inl.ravel().astype(bool)
    if mask.sum() == 0 or not _sane(M):
        return None, 0, float("inf")
    proj = cv2.perspectiveTransform(pb[mask].reshape(-1, 1, 2), M).reshape(-1, 2)
    return M, int(mask.sum()), float(np.median(np.linalg.norm(proj - pa[mask], axis=1)))


def _sane(M: np.ndarray | None) -> bool:
    """A usable B->A transform: finite, invertible, scale within 1/30..30 and a mild perspective. Two unrelated sheets
    can give a RANSAC fit that squeezes every match onto one spot (AFCEC A-2 vs A-101: a singular matrix that crashed
    the compare with LinAlgError)."""
    if M is None or not np.all(np.isfinite(M)):
        return False
    d = abs(float(np.linalg.det(M[:2, :2])))
    return 1e-3 < d < 1e3 and abs(float(np.linalg.det(M))) > 1e-9 and float(np.abs(M[2, :2]).max()) < 1e-2


def _ecc(a: Sheet, b: Sheet, M: np.ndarray, homography: bool) -> np.ndarray | None:
    """Refine B->A on blurred small copies (ECC maximises the correlation of the two sheets' linework)."""
    import cv2

    sa, fa = _small(a.gray, 1400)
    sb, fb = _small(b.gray, 1400)
    S_a, S_b = np.diag([fa, fa, 1.0]), np.diag([fb, fb, 1.0])
    if not _sane(M):
        return None
    Ms = S_a @ M @ np.linalg.inv(S_b)  # B small -> A small
    W = np.linalg.inv(Ms)  # ECC's warp maps template (A) coords -> input (B) coords
    W /= W[2, 2]
    ta = cv2.GaussianBlur(255 - sa, (0, 0), 2.0).astype(np.float32)
    ib = cv2.GaussianBlur(255 - sb, (0, 0), 2.0).astype(np.float32)
    mode = cv2.MOTION_HOMOGRAPHY if homography else cv2.MOTION_AFFINE
    w0 = W.astype(np.float32) if homography else W[:2].astype(np.float32)
    try:
        _, w = cv2.findTransformECC(ta, ib, w0, mode, (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5),
                                    None, 1)
    except cv2.error:
        return None
    W2 = w if homography else np.vstack([w, [0, 0, 1]])
    try:
        Ms2 = np.linalg.inv(W2.astype(np.float64))
    except np.linalg.LinAlgError:
        return None
    out = np.linalg.inv(S_a) @ Ms2 @ S_b
    out = out / out[2, 2]
    return out if _sane(out) else None


def warp_to(a: Sheet, b_img: np.ndarray, M: np.ndarray, fill: int = 255, nearest: bool = False) -> np.ndarray:
    import cv2

    flags = cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR
    return cv2.warpPerspective(b_img, M, (a.gray.shape[1], a.gray.shape[0]), flags=flags,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=fill)


def _overlap(a_ink: np.ndarray, b_ink_w: np.ndarray, valid: np.ndarray, r: int) -> float:
    import cv2

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    a8, b8 = a_ink.astype(np.uint8), b_ink_w.astype(np.uint8)
    da, db = cv2.dilate(a8, k) > 0, cv2.dilate(b8, k) > 0
    na, nb = int((a_ink & valid).sum()), int(b_ink_w.sum())
    if na < 50 or nb < 50:
        return 0.0
    return min(float((a_ink & valid & db).sum()) / na, float((b_ink_w & da).sum()) / nb)


def align(a: Sheet, b: Sheet, a_ink: np.ndarray | None = None, b_ink: np.ndarray | None = None) -> Alignment:
    import cv2

    cv2.setRNGSeed(0)  # RANSAC: the same pair gives the same marks every time
    a_ink = ink_mask(a) if a_ink is None else a_ink
    b_ink = ink_mask(b) if b_ink is None else b_ink
    cands: list[Alignment] = []
    if a.words and b.words:
        pb, pa = _text_pairs(a, b)
        M, n, res = _fit(pb, pa, thr=2.0 * a.zoom)
        if M is not None and n >= 6:
            cands.append(Alignment(M, "text", n, res, note=f"{n} words printed once on both sheets"))
    if not cands or cands[0].residual_px > 2.5:
        pb, pa = _sift_pairs(a, b)
        thr = max(3.0, 0.002 * max(a.gray.shape))
        M, n, res = _fit(pb, pa, thr)
        if M is not None and n >= 12:
            cands.append(Alignment(M, "features", n, res, note=f"{n} SIFT matches"))
        H, nh, resh = _fit(pb, pa, thr, homography=True)
        if H is not None and nh >= 20 and nh > 1.15 * max(n, 1):
            cands.append(Alignment(H, "features+perspective", nh, resh, note=f"{nh} SIFT matches, perspective"))
    best: Alignment | None = None
    for c in cands:
        persp = c.method.endswith("perspective")
        if c.method != "text" or c.residual_px > 1.5:
            M2 = _ecc(a, b, c.M, persp)
            if M2 is not None:
                c2 = Alignment(M2, c.method + "+ecc", c.inliers, c.residual_px, note=c.note + ", ECC refined")
                for x in (c, c2):
                    x.overlap = _score(a, a_ink, b_ink, x.M)
                c = c2 if c2.overlap >= c.overlap else c
            else:
                c.overlap = _score(a, a_ink, b_ink, c.M)
        else:
            c.overlap = _score(a, a_ink, b_ink, c.M)
        if best is None or c.overlap > best.overlap:
            best = c
    if best is None:
        return Alignment(np.eye(3), "none", 0, float("inf"), 0.0, False,
                         "no common words or image features: are these two versions of the same sheet?")
    best.ok = best.overlap >= 0.5
    if not best.ok:
        best.note += f"; only {best.overlap:.0%} of the linework lines up: not treated as the same sheet"
    return best


def _score(a: Sheet, a_ink: np.ndarray, b_ink: np.ndarray, M: np.ndarray) -> float:
    bw = warp_to(a, b_ink.astype(np.uint8), M, fill=0, nearest=True) > 0
    valid = warp_to(a, np.ones(b_ink.shape, np.uint8), M, fill=0, nearest=True) > 0
    return _overlap(a_ink, bw, valid, r=3)


# -------------------------------------------------------------------------------------------------------- diff
_NUM_UNIT = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(m2|m²|sqm|sq\s?m|sf|sq\s?ft|ft2|ft²|mm|cm|m)\b", re.I)


def _quantity(text: str) -> tuple[float, str] | None:
    m = _NUM_UNIT.search(text.replace("²", "2"))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    u = m.group(2).lower().replace(" ", "")
    u = {"m2": "m2", "sqm": "m2", "sf": "ft2", "sqft": "ft2", "ft2": "ft2"}.get(u, u)
    return v, u


def _boxes_hit(box, boxes) -> list[int]:
    x0, y0, x1, y1 = box
    return [i for i, b in enumerate(boxes) if b[0] < x1 and b[2] > x0 and b[1] < y1 and b[3] > y0]


def _box_to_a(a: Sheet, b: Sheet, M: np.ndarray, box) -> tuple[float, float, float, float]:
    """A box in B's native units -> A's native units (through the alignment)."""
    import cv2

    x0, y0, x1, y1 = box
    pts = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) * b.zoom
    q = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), M).reshape(-1, 2) / a.zoom
    return float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max())


# the footer web/server/normalize_upload.py prints on a sheet it draws from a lone DXF upload ('Sheet drawn from
# flat-B.dxf at 1:100 on A3 (no original sheet uploaded)'): ours, not the drawing's; two revisions differ by file name
_MADE_FOOTER = re.compile(r"^\s*sheet drawn from .+? at 1\s*:\s*\d+\b", re.I)


def _line_key(t: str) -> str:
    """A text line's identity for the change list: the made-sheet footer is one line whatever file it names."""
    return "\x00made-sheet-footer" if _MADE_FOOTER.match(t) else _norm_tok(t)


def text_changes(a: Sheet, b: Sheet, M: np.ndarray, tol_pt: float = 3.0) -> list[dict]:
    """Printed text LINES compared in A's native units: same text at the same place = unchanged (not listed); same
    text elsewhere = moved; lines that overlap but read differently = changed ('A: 62 m2' -> 'A: 55 m2'); the rest
    removed (only on A) / added (only on B)."""
    if not (a.lines and b.lines):
        return []
    bl = [(*_box_to_a(a, b, M, ln[:4]), ln[4], ln[:4]) for ln in b.lines]
    tol = tol_pt if a.space == "pdf_pt" else tol_pt / 72 * 150
    diag = math.hypot(*a.native)
    idx: dict[str, list[int]] = collections.defaultdict(list)
    for j, ln in enumerate(bl):
        idx[_line_key(ln[4])].append(j)

    def ctr(bx):
        return (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2

    used_b: set[int] = set()
    lone_a: list[int] = []
    for i, ln in enumerate(a.lines):
        cx, cy = ctr(ln)
        hit = next((j for j in idx.get(_line_key(ln[4]), []) if j not in used_b
                    and abs(ctr(bl[j])[0] - cx) <= tol and abs(ctr(bl[j])[1] - cy) <= tol), None)
        if hit is None:
            lone_a.append(i)
        else:
            used_b.add(hit)
    out: list[dict] = []

    def row(kind, i, j, **kw):
        la = a.lines[i] if i is not None else None
        lb = bl[j] if j is not None else None
        return {"kind": kind, "old": la[4] if la else None, "new": lb[4] if lb else None,
                "box_a": [round(v, 2) for v in la[:4]] if la else None,
                "box_b": [round(v, 2) for v in lb[5]] if lb else None,
                "box_b_in_a": [round(v, 2) for v in lb[:4]] if lb else None, **kw}

    # moved: the same text, nearest unused copy on B (within a fifth of the sheet)
    rest_a = []
    for i in lone_a:
        ln = a.lines[i]
        cx, cy = ctr(ln)
        cands = [j for j in idx.get(_line_key(ln[4]), []) if j not in used_b]
        j = min(cands, key=lambda j: math.dist(ctr(bl[j]), (cx, cy)), default=None)
        if j is not None and math.dist(ctr(bl[j]), (cx, cy)) <= 0.2 * diag:
            used_b.add(j)
            dx, dy = ctr(bl[j])[0] - cx, ctr(bl[j])[1] - cy
            out.append(row("moved", i, j, shift=[round(dx, 2), round(dy, 2)]))
        else:
            rest_a.append(i)
    lone_b = [j for j in range(len(bl)) if j not in used_b]
    paired_b: set[int] = set()
    for i in rest_a:
        ln = a.lines[i]
        grow = (ln[3] - ln[1]) * 0.5
        box = (ln[0] - grow, ln[1] - grow, ln[2] + grow, ln[3] + grow)
        cand = [j for j in lone_b if j not in paired_b and _boxes_hit(box, [bl[j][:4]])]
        if cand:
            j = min(cand, key=lambda j: math.dist(ctr(bl[j]), ctr(ln)))
            paired_b.add(j)
            out.append(row("changed", i, j))
        else:
            out.append(row("removed", i, None))
    for j in lone_b:
        if j not in paired_b:
            out.append(row("added", None, j))
    # the made-sheet footer is never a change, even when the two sheets were drawn at different scales (moved)
    return [t for t in out if not any(_MADE_FOOTER.match(t.get(k) or "") for k in ("old", "new"))]


BLUR_MATCH = os.environ.get("STOA_F5_BLUR", "1") != "0"


def _blur_gap(a: Sheet, b_gray_w: np.ndarray, valid: np.ndarray) -> float:
    """How much blurrier (Gaussian sigma, A render px) the other sheet is than A once lined up: the blur of one side
    that makes the two most alike (illumination and fills removed first). > 0: B is blurrier; < 0: A is."""
    import cv2

    H, W = a.gray.shape
    y0, y1, x0, x1 = H // 6, 5 * H // 6, W // 6, 5 * W // 6
    ga = a.gray[y0:y1, x0:x1].astype(np.float32)
    gb = b_gray_w[y0:y1, x0:x1].astype(np.float32)
    m = valid[y0:y1, x0:x1]
    if m.sum() < 1000:
        return 0.0
    ga -= cv2.GaussianBlur(ga, (0, 0), 8)
    gb -= cv2.GaussianBlur(gb, (0, 0), 8)

    def corr(x, y):
        x, y = x[m], y[m]
        x, y = x - x.mean(), y - y.mean()
        return float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-6))

    best = (corr(ga, gb), 0.0)
    for sg in (0.75, 1.5, 2.25, 3.0, 4.0):
        c = corr(cv2.GaussianBlur(ga, (0, 0), sg), gb)
        if c > best[0]:
            best = (c, sg)
        c = corr(ga, cv2.GaussianBlur(gb, (0, 0), sg))
        if c > best[0]:
            best = (c, -sg)
    return best[1]


DENSE = os.environ.get("STOA_F5_DENSE", "1") != "0"
DENSE_DIFF = float(os.environ.get("STOA_F5_DENSE_DIFF", "0.45"))


def dense_regions(a_ink: np.ndarray, b_ink_w: np.ndarray, valid: np.ndarray, ga: np.ndarray, gb: np.ndarray,
                  slack: int, taken: list) -> list[dict]:
    """Edits INSIDE dense linework (siding, brick, hatching): there every changed pixel is within the tolerance of some
    other line, so the pixel diff is blind. Tiles where BOTH sheets are dense get the label check's patch test (largest
    glyph-sized ink difference after the best offset, at matched blur); 2+ touching tiles over DENSE_DIFF = a region.
    Real HABS sheet vs a rescan: unchanged dense tiles <= 0.14, a label painted over siding 0.48-0.68."""
    import cv2

    H, W = a_ink.shape
    T = max(24, round(0.012 * max(H, W)))
    s_ = slack
    hits = np.zeros((H // T + 1, W // T + 1), np.uint8)
    for y in range(s_, H - T - s_, T):
        for x in range(s_, W - T - s_, T):
            if not valid[y - s_:y + T + s_, x - s_:x + T + s_].all():
                continue
            if min(a_ink[y:y + T, x:x + T].mean(), b_ink_w[y:y + T, x:x + T].mean()) < 0.12:
                continue
            got = _ncc(ga[y:y + T, x:x + T], gb[y - s_:y + T + s_, x - s_:x + T + s_], 1.0, max(5, T // 4))
            if got and got[1] > DENSE_DIFF:
                hits[y // T, x // T] = 1
    if hits.sum() < 2:
        return []
    # flagged tiles one tile apart are one edit (a middle tile can fall just under the bar)
    n, lab, _, _ = cv2.connectedComponentsWithStats(cv2.dilate(hits, np.ones((3, 3), np.uint8)), connectivity=8)
    out = []
    for i in range(1, n):
        ys, xs = np.nonzero((lab == i) & (hits > 0))
        if len(ys) < 2:
            continue
        box = [float(xs.min() * T), float(ys.min() * T), float((xs.max() + 1) * T), float((ys.max() + 1) * T)]
        if any(b[0] < box[2] and b[2] > box[0] and b[1] < box[3] and b[3] > box[1] for b in taken):
            continue
        out.append({"kind": "changed", "px_removed": len(ys), "px_added": len(ys), "box_px": box,
                    "shift_px": None, "dense": True})
    return out


# ---------------------------------------------------------------------- text on scans / photos (cycle 2)
# A renamed label on a scan is invisible to the pixel diff (the new word's strokes lie within the tolerance of the
# old word's). So every printed label box is tested on its own: the grey patch of one sheet is searched for in the
# other (normalised cross-correlation, a few px of slack). Boxes that do not match are OCR'd, ALL IN ONE IMAGE (one
# OCR call: crops stacked with gaps), and only a different reading is a change. Full-sheet OCR is no option: a
# 150-dpi scan's room labels are ~9 px tall (PP-OCR reads none) and a 300-dpi sheet is over the 120 s budget.
TEXT_OCR = os.environ.get("STOA_F5_OCR", "1") != "0"
TEXT_NCC = float(os.environ.get("STOA_F5_TEXT_NCC", "0.6"))  # a label patch under this correlation is OCR'd …
TEXT_DIFF = float(os.environ.get("STOA_F5_TEXT_DIFF", "0.3"))  # … or with a glyph-sized difference over this
MAX_CROPS = 48


def _label_like(t: str) -> bool:
    """Printed text worth checking on a scan: 2+ letters / digits (single characters — grid bubbles, door tags, a
    dimension's digit — gave most of the false readings on phone photos: '9' read '6', a wall read 'I')."""
    s = t.replace(" ", "")
    n = sum(c.isalnum() for c in s)
    return 2 <= len(s) <= 40 and n >= 2 and n >= 0.6 * len(s)


OCR_MIN_SCORE = float(os.environ.get("STOA_F5_OCR_SCORE", "0.8"))  # a reading below this is 'could not be read'
SMALL_DIGITS_PX = float(os.environ.get("STOA_F5_SMALL_DIGITS", "16"))  # word box px: a number read from smaller text is a guess


def _reading(r: tuple[str, float]) -> tuple[str, float]:
    """An OCR reading with fewer than 2 letters / digits is no reading ('..', a wall line read as '1')."""
    t = r[0].strip().strip("._-:;,'\"|~ ")
    return (t, r[1]) if sum(c.isalnum() for c in t) >= 2 else ("", 0.0)


def _blob_boxes(ink: np.ndarray) -> list[list[float]]:
    """Word-like groups of glyph-sized ink blobs (render px): the label candidates when neither sheet has a text
    layer (scan vs scan)."""
    import cv2

    L = max(ink.shape)
    hmin, hmax = max(4, round(0.0012 * L)), round(0.012 * L)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    hs = []
    for i in range(1, n):
        w_, h_, ar = st[i, 2], st[i, 3], st[i, 4]
        if hmin <= h_ <= hmax and w_ <= 2.5 * h_ and ar >= 0.12 * w_ * h_:
            keep[i] = True
            hs.append(h_)
    if not hs:
        return []
    gh = float(np.median(hs))
    glyphs = keep[lab].astype(np.uint8)
    grown = cv2.dilate(glyphs, cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, round(0.9 * gh)), 1)))
    n2, _, st2, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    out = []
    for i in range(1, n2):
        x, y, w_, h_, _ = st2[i]
        if hmin <= h_ <= 1.6 * hmax and 1.2 * h_ <= w_ <= 0.25 * L:
            out.append([float(x), float(y), float(x + w_), float(y + h_)])
    return out


class _Src:
    """One sheet's own pixels for label patches: the full-resolution image (scan / photo) or the PDF page (vector)."""

    def __init__(self, s: Sheet):
        self.s, self.pg, self.doc, self.dl = s, None, None, None
        self.img = _full_image(s.path) if s.space == "image_px" else None

    def page(self):
        if self.pg is None:
            import fitz

            self.doc = fitz.open(self.s.path)
            self.pg = self.doc[self.s.page or 0]
            self.pg.set_rotation(0)
            self.dl = self.pg.get_displaylist()  # parsed once: each label's clip renders from it
        return self.pg

    def close(self) -> None:
        if self.doc is not None:
            self.doc.close()


def _local_patch(src: _Src, G: np.ndarray, box, sc: float, margin: float) -> np.ndarray | None:
    """The sheet's pixels over A's native `box` grown by `margin` (A native units), sampled at `sc` px per A unit
    (text ~28 px tall, upright as on A, any rotation / perspective of the scan undone). G: A native -> the sheet's
    native coordinates."""
    import cv2

    x0, y0, x1, y1 = box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin
    w, h = round((x1 - x0) * sc), round((y1 - y0) * sc)
    if w < 6 or h < 6 or w > 2400 or h > 2400:
        return None
    GT = G @ np.array([[1 / sc, 0, x0], [0, 1 / sc, y0], [0, 0, 1]])  # patch px -> sheet native
    if src.img is not None:
        img, S = src.img, np.eye(3)
        Hs, Ws = img.shape
    else:
        import fitz

        q = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), GT).reshape(-1, 2)
        (cx0, cy0), (cx1, cy1) = q.min(0) - 2, q.max(0) + 2
        z = float(np.clip(max(w / max(cx1 - cx0, 1e-6), h / max(cy1 - cy0, 1e-6)), 0.5, 40))
        pg = src.page()
        clip = fitz.Rect(cx0 + pg.rect.x0, cy0 + pg.rect.y0, cx1 + pg.rect.x0, cy1 + pg.rect.y0) & pg.rect
        if clip.is_empty:
            return None
        pix = src.dl.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY, alpha=False, clip=clip)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
        S = np.array([[z, 0, pg.rect.x0 * z - pix.x], [0, z, pg.rect.y0 * z - pix.y], [0, 0, 1]])
        Hs, Ws = (src.s.native[1], src.s.native[0])
        Hs, Ws = Hs * z, Ws * z
    c = np.array([(x0 + x1) / 2, (y0 + y1) / 2, 1.0])
    cc = S @ G @ c
    cc = cc[:2] / cc[2]
    if src.img is not None and not (0 <= cc[0] < Ws and 0 <= cc[1] < Hs):
        return None
    return cv2.warpPerspective(img, S @ GT, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def _ncc(pa: np.ndarray, pb: np.ndarray, sigma: float, glyph: int) -> tuple[float, float] | None:
    """(best correlation of A's patch inside B's bigger one, largest glyph-sized difference at that offset) after the
    same blur; None = both blank. The local difference catches one changed glyph in a long label ('1985' -> '1992',
    'AIR FORCE' -> 'AIR PLANT') that barely moves the correlation of the whole patch."""
    import cv2

    a_ = cv2.GaussianBlur(pa, (0, 0), sigma).astype(np.float32)
    b_ = cv2.GaussianBlur(pb, (0, 0), sigma).astype(np.float32)
    if a_.std() < 6 and b_.std() < 6:
        return None
    if a_.std() < 3 or b_.std() < 3:  # one side blank: the label is gone (or new)
        return 0.0, 1.0
    r = cv2.matchTemplate(b_, a_, cv2.TM_CCOEFF_NORMED)
    _, v, _, (ox, oy) = cv2.minMaxLoc(r)
    bb = b_[oy:oy + a_.shape[0], ox:ox + a_.shape[1]]

    def ink(x):  # 0 = paper, 1 = ink, per patch (scans are greyer than renders)
        lo, hi = np.percentile(x, 3), np.percentile(x, 90)
        return np.clip((hi - x) / max(hi - lo, 20.0), 0, 1)

    d = cv2.blur(np.abs(ink(a_) - ink(bb)), (glyph, glyph))
    m = max(1, glyph // 2)
    d = d[m:-m, m:-m] if d.shape[0] > 2 * m + 2 and d.shape[1] > 2 * m + 2 else d
    return float(v), float(d.max())


_FULL: dict[str, np.ndarray] = {}


def _full_image(path: str) -> np.ndarray:
    import cv2

    if path not in _FULL:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None
            img = np.asarray(Image.open(path).convert("L"))
        _FULL.clear()  # one sheet at a time (a 300-dpi A1 scan is ~100 MB)
        _FULL[path] = img
    return _FULL[path]


OCR_SERVER_WAIT = float(os.environ.get("STOA_F5_OCR_WAIT", "6"))  # s; a busy shared server -> run the engine here


def _ocr_image(path: Path) -> dict:
    """PP-OCR of one small image: cache -> the shared OCR server if it answers within OCR_SERVER_WAIT s (it queues
    behind whole-sheet reads) -> a one-off engine process (~8 s with the model load)."""
    import subprocess
    import urllib.request

    from stoa import ocr

    f = path.with_suffix(".json")
    if f.exists():
        return {**json.loads(f.read_text()), "cached": True}
    d = None
    if ocr.server_up():
        req = urllib.request.Request(ocr.OCR_URL, data=json.dumps({"path": str(path)}).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=OCR_SERVER_WAIT) as r:
                d = json.loads(r.read())
        except Exception:  # noqa: BLE001 — busy or down
            d = None
    if d is None or "error" in d:
        if not Path(ocr.OCR_PY).exists():
            raise RuntimeError("the OCR server is busy and no local OCR engine is installed")
        env = {**os.environ, "OMP_NUM_THREADS": os.environ.get("STOA_OCR_THREADS", "4")}
        out = subprocess.run([ocr.OCR_PY, str(ocr.ENGINE_FILE), str(path)], capture_output=True, text=True,
                             timeout=90, env=env)
        lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise RuntimeError("OCR engine failed: " + out.stderr[-200:])
        d = json.loads(lines[-1])
        if "error" in d:
            raise RuntimeError(str(d["error"]))
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.replace(tmp, f)
    return d


def ocr_crops(crops: list[np.ndarray]) -> tuple[list[tuple[str, float]], dict]:
    """OCR many small crops with ONE engine call: stacked into one image with white gaps; each reading goes back to
    the crop its centre falls in. Returns [(text, mean score)] per crop (('', 0) = nothing read) and cost info."""
    import cv2

    gap = 40
    W = max(c.shape[1] for c in crops) + 2 * gap
    Ht = sum(c.shape[0] + gap for c in crops) + gap
    canvas = np.full((Ht, W), 255, np.uint8)
    spans, y = [], gap
    for c in crops:
        canvas[y:y + c.shape[0], gap:gap + c.shape[1]] = c
        spans.append((y - gap / 2, y + c.shape[0] + gap / 2))
        y += c.shape[0] + gap
    ok, buf = cv2.imencode(".png", canvas)
    tag = hashlib.sha1(buf.tobytes()).hexdigest()[:16]
    d_ = CACHE_DIR / "ocr"
    d_.mkdir(parents=True, exist_ok=True)
    f = d_ / f"crops.{tag}.png"
    if not f.exists():
        tmp = f.with_suffix(".tmp.png")
        tmp.write_bytes(buf.tobytes())
        os.replace(tmp, f)
    d = _ocr_image(f)
    got: list[list] = [[] for _ in crops]
    for it in d.get("items", []):
        x0, y0, x1, y1 = (float(v) for v in it["box"])
        cy = (y0 + y1) / 2
        k = next((i for i, (a_, b_) in enumerate(spans) if a_ <= cy < b_), None)
        if k is not None and it.get("text", "").strip():
            got[k].append((y0 // 20, x0, str(it["text"]).strip(), float(it.get("score", 1.0))))
    out = []
    for g in got:
        g.sort()
        out.append((" ".join(t for *_, t, _ in g), float(np.mean([s_ for *_, s_ in g])) if g else 0.0))
    return out, {"crops": len(crops), "cpu_seconds": 0.0 if d.get("cached") else float(d.get("seconds", 0.0)),
                 "engine": d.get("engine", ""), "image": f.name}


def _short_misread(x: str, y: str, score: float) -> bool:
    """A 'rename' where either side has fewer than 3 letters / digits (room numbers, grid marks) counts only if the OCR
    was very sure (>= 0.95) and both sides are the same kind of text: digits -> digits ('11' -> '12') or letters ->
    letters. A clean rescan of the DfE ground floor gave '11' -> 'FI' at 0.8+ otherwise."""
    nx, ny = re.sub(r"[^0-9A-Za-z]", "", x), re.sub(r"[^0-9A-Za-z]", "", y)
    if min(len(nx), len(ny)) >= 3:
        return False
    kind = lambda t: "d" if t.isdigit() else "a" if t.isalpha() else "m"  # noqa: E731
    return score < 0.95 or kind(nx) != kind(ny)


_DICT: set[str] | None = None


def _dictionary() -> set[str]:
    """English words (the system word lists, lower case; empty when none is installed: the rule then stays off)."""
    global _DICT
    if _DICT is None:
        words: set[str] = set()
        for f in ("/usr/share/dict/british-english", "/usr/share/dict/american-english", "/usr/share/dict/words"):
            try:
                words.update(w.strip().lower() for w in open(f, encoding="utf-8", errors="ignore") if w.strip())
            except OSError:
                pass
        _DICT = {re.sub(r"[^a-z]", "", w) for w in words}
    return _DICT


def _wordlike(t: str, vocab: dict[str, str]) -> bool:
    """Every 3+-letter word of an OCR reading is a word: the sheets' own, a room word, English (plural too), or a short
    acronym in capitals ('SWR'). 'Abom', 'Chetr', 'ine acicn' are OCR misreads of blurred labels, not renames
    (F5 noise pairs: 6 of 8 false 'renamed label' rows on 16 PDF-vs-own-scan pairs)."""
    import difflib

    d = _dictionary()
    if not d:
        return True
    for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", t):  # glued words split at a case change ('KitcheneWALL')
        k = w.lower()
        if len(k) < 3 or k in vocab or k in d or (k.endswith("s") and k[:-1] in d) or (w.isupper() and len(w) <= 4):
            continue
        if len(k) >= 5 and difflib.get_close_matches(k, vocab.keys(), n=1, cutoff=0.8):
            continue  # a known word with a glyph or two misread ('Kitchene' for 'Kitchenette'): readable enough
        return False
    return True


def _spillover(old: str, new: str, near: set[str]) -> bool:
    """One reading is the other plus words printed right next to the label ('Music' -> 'Music 2' where the room number
    '2' sits beside it): the OCR crop took in a neighbour, nothing was renamed."""
    a, b = old.split(), new.split()
    if len(a) > len(b):
        a, b = b, a
    if not a or a != b[:len(a)] and a != b[len(b) - len(a):]:
        return False
    extra = b[len(a):] if a == b[:len(a)] else b[:len(b) - len(a)]
    return bool(extra) and all(re.sub(r"[^0-9a-z]", "", w.lower()) in near for w in extra)


def _same_text(x: str, y: str) -> bool:
    """OCR noise is not a change: equal after normalising or up to glyphs OCR confuses, or the same digits and ≥ 75 %
    alike otherwise."""
    import difflib

    nx, ny = re.sub(r"[^0-9a-z]", "", x.lower()), re.sub(r"[^0-9a-z]", "", y.lower())
    if nx == ny:
        return True
    conf = str.maketrans("0o1li5s8b", "oolllssbb")  # glyphs OCR mixes up ('0FFICE')
    if nx.translate(conf) == ny.translate(conf):
        return True
    if re.sub(r"\D", "", nx) != re.sub(r"\D", "", ny):  # 'Yr 4' -> 'Yr 5', '62 m2' -> '55 m2': a change
        return False
    return difflib.SequenceMatcher(None, nx, ny).ratio() >= 0.75


ROOM_WORDS = ("Store Storage Office Kitchen Kitchenette Classroom Toilet Toilets WC Plant Corridor Stair Stairs Lobby Hall "
              "Reception Meeting Bedroom Bathroom Living Dining Laundry Utility Cleaner Server Studio Workshop Library "
              "Staff Room Entrance Foyer Lift Shower Changing Hygiene Medical Therapy Group Food Servery Practical "
              "Science Music Hall Sports Garage Balcony Terrace Closet Pantry Mechanical Electrical Janitor Vestibule "
              "Conference Break Lounge Open Area Unit Flat Apartment").split()


def _snap(text: str, vocab: dict[str, str]) -> str:
    """OCR words snapped to a known word when nearly equal ('Starage' -> 'Storage'): the vector sheet's own words
    and common room names. Words with digits are never snapped (a number change must stay a change)."""
    import difflib

    out = []
    for w in text.split():
        k = re.sub(r"[^a-z]", "", w.lower())
        if len(k) >= 4 and not re.search(r"\d", w) and k not in vocab:
            m = difflib.get_close_matches(k, vocab.keys(), n=1, cutoff=0.8)
            if m:
                w = vocab[m[0]]
        out.append(w)
    return " ".join(out)


def scan_text_changes(a: Sheet, b: Sheet, M: np.ndarray, slack: int) -> tuple[list[dict], list, dict]:
    """Text changes when at least one sheet has no text layer (a scan or a photo). Candidates: the text-layer lines of
    the vector sheet, or word-like ink blobs on both (scan vs scan). Each is compared in its own small frame (text ~28
    px tall, both sheets resampled from their own full-resolution pixels), and only the ones that differ are OCR'd.
    Returns (changes in text_changes' row format, boxes in A render px found unchanged — masked out of the pixel
    diff —, info)."""
    import cv2

    info: dict = {"candidates": 0, "checked": 0, "ocr": None, "note": None}
    if not TEXT_OCR:
        info["note"] = "text check on scans is off (STOA_F5_OCR=0)"
        return [], [], info
    Da, Db = np.diag([a.zoom, a.zoom, 1.0]), np.diag([b.zoom, b.zoom, 1.0])
    Gb = np.linalg.inv(Db) @ np.linalg.inv(M) @ Da  # A native -> B native
    cands = []  # (box in A native, text on A or None, text on B or None)
    # the vector sheet's printed WORDS (one changed word in a long line stands out on its own)
    if a.kind == "vector":
        cands += [(list(w[:4]), w[4], None) for w in a.words if _label_like(w[4])]
    if b.kind == "vector":
        cands += [(list(_box_to_a(a, b, M, w[:4])), None, w[4]) for w in b.words if _label_like(w[4])]
    if a.kind != "vector" and b.kind != "vector":
        boxes = _blob_boxes(ink_mask(a, "strong", False))
        b_ink_w = warp_to(a, ink_mask(b, "strong", False).astype(np.uint8), M, fill=0, nearest=True) > 0
        for bx in _blob_boxes(b_ink_w):
            if not any(_iou(bx, o) > 0.3 for o in boxes):
                boxes.append(bx)
        cands += [([v / a.zoom for v in bx], None, None) for bx in boxes]
    info["candidates"] = len(cands)
    vec_boxes = [c[0] for c in cands if c[1] is not None or c[2] is not None]
    sa, sb = _Src(a), _Src(b)
    slack_n = slack / a.zoom
    flagged, same = [], []
    try:
        for bx, ta, tb in cands:
            th = max(1e-6, min(bx[2] - bx[0], bx[3] - bx[1]))
            sc = 28 / th
            if (bx[2] - bx[0]) * sc > 1600 or (bx[3] - bx[1]) * sc > 1600:
                sc = 1600 / max(bx[2] - bx[0], bx[3] - bx[1])
            m = 0.3 * th
            pa = _local_patch(sa, np.eye(3), bx, sc, m)
            pb = _local_patch(sb, Gb, bx, sc, m + slack_n)
            if pa is None or pb is None:
                continue
            got = _ncc(pa, pb, 0.06 * th * sc, glyph=max(5, round(0.6 * th * sc)))
            if got is None:
                continue
            v, dmax = got
            info["checked"] += 1
            if v < TEXT_NCC or dmax > TEXT_DIFF:
                k0 = round(slack_n * sc)
                pb_c = pb[k0:pb.shape[0] - k0, k0:pb.shape[1] - k0] if k0 else pb
                if (ta is not None or tb is not None) and bx[2] - bx[0] >= bx[3] - bx[1]:
                    # the scan's word may be LONGER than the printed one it replaced: read on to the next printed word
                    # of the same line (at most 2.5 text heights)
                    cy = (bx[1] + bx[3]) / 2
                    nxt = min((o[0] for o in vec_boxes if o[0] > bx[2] and abs((o[1] + o[3]) / 2 - cy) < 0.3 * th),
                              default=bx[2] + 2.5 * th)
                    ext = min(nxt - bx[2] - 0.3 * th, 2.5 * th)
                    if ext > 0.3 * th:
                        big = [bx[0], bx[1], bx[2] + ext, bx[3]]
                        p2 = _local_patch(sa if tb is not None else sb, np.eye(3) if tb is not None else Gb, big, sc, m)
                        if p2 is not None:
                            if ta is not None:
                                pb_c = p2
                            else:
                                pa = p2
                flagged.append((v, bx, ta, tb, pa, pb_c))
            else:
                same.append(bx)
    finally:
        sa.close()
        sb.close()
    flagged.sort(key=lambda r_: r_[0])
    if len(flagged) > MAX_CROPS:
        info["note"] = f"{len(flagged)} labels look different; the {MAX_CROPS} most different were read"
        flagged = flagged[:MAX_CROPS]
    px = [[v * a.zoom for v in bx] for bx in same]
    rows: list[dict] = []
    if not flagged:
        return rows, px, info

    def upright(c, bx):
        return cv2.rotate(c, cv2.ROTATE_90_CLOCKWISE) if (bx[3] - bx[1]) > 1.6 * (bx[2] - bx[0]) else c

    jobs, slots = [], []  # crops to OCR; per flagged label: (index of A's crop or None, index of B's crop or None)
    for v, bx, ta, tb, pa, pb in flagged:
        ia = ib = None
        if ta is None:
            ia = len(jobs)
            jobs.append(upright(pa, bx))
        if tb is None:
            ib = len(jobs)
            jobs.append(upright(pb, bx))
        slots.append((ia, ib))
    try:
        reads, cost = ocr_crops(jobs)
    except Exception as e:  # noqa: BLE001 — no OCR: the pixel diff stays the only text signal
        info["note"] = f"labels on the scan could not be read ({str(e)[:80]}): renamed labels may be missed"
        return rows, px, info
    info["ocr"] = cost

    def native_b(bx):
        q = cv2.perspectiveTransform(np.float32([[bx[0], bx[1]], [bx[2], bx[1]], [bx[2], bx[3]], [bx[0], bx[3]]])
                                     .reshape(-1, 1, 2), Gb).reshape(-1, 2)
        return [float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max())]

    vocab = {re.sub(r"[^a-z]", "", w.lower()): w for w in ROOM_WORDS}
    vocab.update({re.sub(r"[^a-z]", "", w[4].lower()): w[4].strip(".,;:()") for s_ in (a, b) for w in s_.words
                  if w[4].isalpha() and len(w[4]) >= 4})
    # the vector sheet's printed words (all of them, single digits too) in A's frame: a reading's neighbours
    vec_words = [(list(w[:4]), re.sub(r"[^0-9a-z]", "", w[4].lower())) for w in a.words] if a.words else \
        [(list(_box_to_a(a, b, M, w[:4])), re.sub(r"[^0-9a-z]", "", w[4].lower())) for w in b.words]
    vec_words = [x for x in vec_words if x[1]]
    unread = 0
    for (v, bx, ta, tb, pa, pb), (ia, ib) in zip(flagged, slots):
        ra = (ta, 1.0) if ta is not None else _reading((_snap(reads[ia][0], vocab), reads[ia][1]))
        rb = (tb, 1.0) if tb is not None else _reading((_snap(reads[ib][0], vocab), reads[ib][1]))
        old, new = ra[0].strip(), rb[0].strip()
        def known(t):  # every word of the reading is a known word (after snapping): the vocabulary backs a lower score
            ks = [re.sub(r"[^a-z]", "", w.lower()) for w in t.split()]
            return bool(ks) and all(k in vocab for k in ks if k) and any(ks)

        def low(t, sc_, ocr_read):
            return ocr_read and t and sc_ < (OCR_MIN_SCORE - 0.1 if known(t) else OCR_MIN_SCORE)

        if low(old, ra[1], ta is None) or low(new, rb[1], tb is None):
            unread += 1
            continue
        if not old and not new:
            continue
        if old and new and _same_text(old, new):
            px.append([x * a.zoom for x in bx])
            continue
        if old and new and _short_misread(old, new, min(ra[1] if ta is None else 1.0, rb[1] if tb is None else 1.0)):
            unread += 1  # '11' -> 'FI' on an unchanged sheet: OCR on a 2-glyph crop is not evidence of a rename
            continue
        if old and new:
            h = max(1e-6, min(bx[2] - bx[0], bx[3] - bx[1]))
            near = {t_ for w, t_ in vec_words
                    if w[0] < bx[2] + 3 * h and w[2] > bx[0] - 3 * h and w[1] < bx[3] + 3 * h and w[3] > bx[1] - 3 * h}
            if _spillover(old, new, near):
                px.append([x * a.zoom for x in bx])
                continue
        if (ta is None and old and not _wordlike(old, vocab)) or (tb is None and new and not _wordlike(new, vocab)):
            unread += 1  # a reading that is no word at all: OCR could not read the blurred label
            continue
        if old and new and not re.search(r"[A-Za-z]", old + new):
            nb = native_b(bx)
            hb = min(nb[2] - nb[0], nb[3] - nb[1]) * (b.zoom if b.space == "pdf_pt" else 1.0)
            shape = lambda t: re.sub(r"\d", "9", re.sub(r"\s", "", t))  # noqa: E731  '7,200' -> '9,999'
            if hb < SMALL_DIGITS_PX and shape(old) != shape(new):
                unread += 1  # '7,200' -> '2200' / '120' on 14-px text: a separator or digit lost = a misread
                continue  # ('48' -> '55', '1965' -> '1972' keep their shape: real edits on small text stay)
        if not old or not new:  # one side read nothing: only a near-blank patch means the label is really gone
            blank = pa if not old else pb
            if float(blank.std()) > 12:
                unread += 1
                continue
        kind = "changed" if old and new else "removed" if old else "added"
        rows.append({"kind": kind, "old": old or None, "new": new or None,
                     "box_a": [round(x, 2) for x in bx] if old else None,
                     "box_b": [round(x, 2) for x in native_b(bx)] if new else None,
                     "box_b_in_a": [round(x, 2) for x in bx], "source": "read from the scan by OCR", "ncc": round(v, 2)})
    if unread:
        info["note"] = (info["note"] + "; " if info["note"] else "") + f"{_n(unread, 'label')} differ but could not be read"
    if unread >= 10 and unread >= 0.8 * len(flagged):  # the copy is too coarse for its labels: say so, plainly
        hs = sorted(min(f_[1][2] - f_[1][0], f_[1][3] - f_[1][1]) for f_ in flagged)
        raster = b if a.kind == "vector" else a
        k = math.sqrt(abs(np.linalg.det(Gb[:2, :2]))) if raster is b else 1.0  # raster px per A native unit
        tpx = hs[len(hs) // 2] * k
        info["note"] = (f"the labels on this scan / photo are too small to read (text ~{tpx:.0f} px tall; OCR reads ~10 px and up): "
                        f"{len(flagged)} labels differ by pixels but could not be read, so renamed labels may be "
                        "missed. A 300-dpi scan or a closer photo reads them.")
    return rows, px, info


def _iou(p, q) -> float:
    ix = max(0.0, min(p[2], q[2]) - max(p[0], q[0]))
    iy = max(0.0, min(p[3], q[3]) - max(p[1], q[1]))
    i = ix * iy
    u = (p[2] - p[0]) * (p[3] - p[1]) + (q[2] - q[0]) * (q[3] - q[1]) - i
    return i / u if u > 0 else 0.0


# 'A: 62 m2', '468 SF', and in brackets '(55.0 m2)' (DfE 2012 baseline designs print the area under the name that way)
_AREA_LINE = re.compile(r"^[(\[]?\s*(?:(?:[A-Za-z]{1,4}|area|nia|gia)\s*[:=]\s*)?(?P<v>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*"
                        r"(?P<u>m2|m²|sq\.?\s?m|sqm|sf|s\.f\.|sq\.?\s?ft\.?|ft2|ft²)\s*[)\]]?$", re.I)


def _upright_copy(path: str, page: int) -> tuple[str, np.ndarray] | None:
    """A PDF page whose text mostly runs sideways or upside down (a sheet re-plotted turned 90 / 180 / 270 degrees):
    a copy turned back upright (cached) and the 2x3 affine from the copy's points to the page's. None = upright.
    Room-tag readers pair a name with the number BELOW it: on the DfE first floor turned 90 degrees they paired the
    wrong ones and 31 of 32 rooms 'changed' (cost -EUR 835 instead of +EUR 380)."""
    import cv2
    import fitz

    with fitz.open(path) as doc:
        pg = doc[page]
        if pg.rotation:  # a sheet stored with a /Rotate flag reads upright on screen: the readers handle it already
            return None   # (70 of 197 dataset pages, e.g. the AFCEC dorm set; only content drawn turned is turned here)
        dirs: collections.Counter = collections.Counter()
        for blk in pg.get_text("dict").get("blocks", []):
            for ln in blk.get("lines", []):
                dx, dy = ln["dir"]
                dirs[round(math.degrees(math.atan2(dy, dx)) / 90) % 4] += sum(len(sp["text"].strip()) for sp in ln["spans"])
        k, n = dirs.most_common(1)[0] if dirs else (0, 0)
        if k == 0 or n < 0.6 * sum(dirs.values()) or n < 40:
            return None
        st = os.stat(path)
        key = hashlib.sha256(json.dumps([str(path), st.st_mtime_ns, st.st_size, page, 1]).encode()).hexdigest()[:16]
        out_p = CACHE_DIR / "upright" / f"{key}.pdf"
        if not out_p.exists():
            out_p.parent.mkdir(parents=True, exist_ok=True)
            for rot in (k * 90, 360 - k * 90):  # whichever turn brings the text upright
                nd = fitz.open()
                w, h = pg.rect.width, pg.rect.height
                npg = nd.new_page(width=h if k % 2 else w, height=w if k % 2 else h)
                npg.show_pdf_page(npg.rect, doc, page, rotate=rot)
                d2: collections.Counter = collections.Counter()
                for blk in npg.get_text("dict").get("blocks", []):
                    for ln in blk.get("lines", []):
                        dx, dy = ln["dir"]
                        d2[round(math.degrees(math.atan2(dy, dx)) / 90) % 4] += 1
                if d2 and d2.most_common(1)[0][0] == 0:
                    tmp = out_p.with_suffix(".tmp.pdf")
                    nd.save(str(tmp))
                    os.replace(tmp, out_p)
                    break
            else:
                return None
        words_b = pg.get_text("words")
    with fitz.open(str(out_p)) as nd:
        words_u = nd[0].get_text("words")
    cb = collections.Counter(w[4] for w in words_b)
    cu = collections.Counter(w[4] for w in words_u)
    pb = {w[4]: ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) for w in words_b if cb[w[4]] == 1 and len(w[4]) >= 3}
    pu = {w[4]: ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2) for w in words_u if cu[w[4]] == 1 and w[4] in pb}
    if len(pu) < 6:
        return None
    src = np.float32([pu[t] for t in pu])
    dst = np.float32([pb[t] for t in pu])
    A, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.0)
    return (str(out_p), A) if A is not None else None


def _map_box(A: np.ndarray, box) -> list[float]:
    x0, y0, x1, y1 = box
    pts = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]], float) @ A.T
    return [float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())]


def area_tags(s: Sheet) -> list[dict]:
    """Printed room areas: a line '<value> <unit>' (optionally 'A: 62 m2', 'U106 - 468 SF' via the area_tags
    solver's reader) and the name line just above it. Reuses stoa.solvers.area_tags.read_tags on PDFs when it finds
    tags; a lenient local reading otherwise (it misses the DfE baseline sheets' 'A: 62 m2', 0 tags)."""
    if s.space == "pdf_pt":
        try:
            from stoa.solvers.area_tags import read_tags

            up = _upright_copy(s.path, s.page or 0) if UPRIGHT else None
            if up:  # a sheet re-plotted turned: read its tags upright, boxes back on the page
                tags = read_tags(up[0], 0)
                if len(tags) >= 3:
                    return [{"name": t.get("name") or t.get("number") or "", "value": t["value"], "unit": t["unit"],
                             "box": _map_box(up[1], t["bbox"]), "tag_box": _map_box(up[1], t["tag_bbox"])} for t in tags]
            tags = read_tags(s.path, s.page or 0)
            if len(tags) >= 3:
                return [{"name": t.get("name") or t.get("number") or "", "value": t["value"], "unit": t["unit"],
                         "box": list(t["bbox"]), "tag_box": list(t["tag_bbox"])} for t in tags]
        except Exception:  # noqa: BLE001
            pass
    out = []
    for x0, y0, x1, y1, t in s.lines:
        m = _AREA_LINE.match(t.strip())
        if not m:
            continue
        v = float(m.group("v").replace(",", ""))
        ul = m.group("u").lower().replace(" ", "").replace(".", "")
        u = "ft2" if ul in ("sf", "sqft", "ft2", "ft²") else "m2"
        h = y1 - y0
        cx = (x0 + x1) / 2
        lo = -0.6 * h if s.space == "image_px" else 0.0  # OCR boxes of a scan overlap the line below by ~40 %
        above = [ln for ln in s.lines if lo <= y0 - ln[3] <= 1.2 * h + 1 and ln[1] < y0
                 and (ln[1] + ln[3]) / 2 < (y0 + y1) / 2 - 0.25 * h
                 and abs((ln[0] + ln[2]) / 2 - cx) <= max(x1 - x0, ln[2] - ln[0]) * 0.75
                 and not _AREA_LINE.match(ln[4].strip()) and sum(c.isalpha() for c in ln[4]) >= 2]
        name_ln = max(above, key=lambda ln: ln[3], default=None)
        if name_ln is None:
            continue  # a total in a legend ('GF Area 1645m2' is one line) or a dimension, not a room tag
        box = [min(x0, name_ln[0]), name_ln[1], max(x1, name_ln[2]), y1]
        out.append({"name": name_ln[4].strip(), "value": v, "unit": u, "box": box, "tag_box": [x0, y0, x1, y1]})
    return out


OCR_AREAS = os.environ.get("STOA_F5_OCR_AREAS", "1") != "0"  # room tags on scans from the upload's OCR text layer
LAYER_WAIT_S = float(os.environ.get("STOA_F5_LAYER_WAIT", "180"))  # a just-uploaded scan: its text layer may follow


def layer_lines(s: Sheet) -> list[tuple] | None:
    """A scan's printed lines from the text layer the upload warm-up read (stoa.formats.warm; memory or disk cache,
    never a cold read here), in the sheet's own image px. Tiles overlap: an item mostly covered by an earlier one is
    dropped ('Yf 5' under 'Yr 5' is one label). None = not read (yet)."""
    if s.space != "image_px" or not OCR_AREAS:
        return None
    try:
        from stoa.solvers.raster.sheet import cached_layer

        layer = cached_layer(s.path)
    except Exception:  # noqa: BLE001
        return None
    if layer is None:
        return None
    lw, lh = (layer.size or s.native)[:2]
    fx, fy = s.native[0] / max(lw, 1), s.native[1] / max(lh, 1)  # a layer read on a resized copy
    kept: list[tuple] = []
    for it in layer.items:
        t = re.sub(r"\s+", " ", it.text or "").strip()
        if not t:
            continue
        if re.search(r"\d\s*(?:m2|m²|sqm)$", t, re.I):  # OCR'd area tags: 'A 55 m2', 'A.4 m2', 'A8m2' -> 'A: 55 m2'
            t = re.sub(r"^([A-Za-z])\s*[.;,:]?\s*(?=\d)", r"\1: ", t)
        b = (it.box[0] * fx, it.box[1] * fy, it.box[2] * fx, it.box[3] * fy)
        ar = max((b[2] - b[0]) * (b[3] - b[1]), 1.0)
        if any(max(0.0, min(b[2], k[2]) - max(b[0], k[0])) * max(0.0, min(b[3], k[3]) - max(b[1], k[1]))
               > 0.5 * min(ar, max((k[2] - k[0]) * (k[3] - k[1]), 1.0)) for k in kept):
            continue
        kept.append((*b, t))
    return kept


class PatchTest:
    """Is the printed text in this box (A native units) the same on both sheets, by their own pixels? The label check's
    test (scan_text_changes), for one box at a time: True / False / None (could not be compared)."""

    def __init__(self, a: Sheet, b: Sheet, M: np.ndarray, slack_px: float):
        Da, Db = np.diag([a.zoom, a.zoom, 1.0]), np.diag([b.zoom, b.zoom, 1.0])
        self.Gb = np.linalg.inv(Db) @ np.linalg.inv(M) @ Da
        self.a, self.sa, self.sb, self.slack = a, _Src(a), _Src(b), slack_px / a.zoom

    def __call__(self, bx, strict: bool = False) -> bool | None:
        """strict: True only when clearly identical (correlation >= 0.9, largest glyph difference <= 0.2): at 4-8 pt
        text on a 150-dpi scan one changed digit ('12' -> '10') scores 0.26 and an unchanged one 0.30, so the label
        check's own threshold (0.3) cannot overrule an OCR reading."""
        th = max(1e-6, min(bx[2] - bx[0], bx[3] - bx[1]))
        sc = 28 / th
        if max(bx[2] - bx[0], bx[3] - bx[1]) * sc > 1600:
            sc = 1600 / max(bx[2] - bx[0], bx[3] - bx[1])
        m = 0.3 * th
        try:
            pa = _local_patch(self.sa, np.eye(3), bx, sc, m)
            pb = _local_patch(self.sb, self.Gb, bx, sc, m + self.slack)
            got = None if pa is None or pb is None else _ncc(pa, pb, 0.06 * th * sc, glyph=max(5, round(0.6 * th * sc)))
        except Exception:  # noqa: BLE001
            return None
        if got is None:
            return None
        if strict:
            return got[0] >= 0.9 and got[1] <= 0.2
        return not (got[0] < TEXT_NCC or got[1] > TEXT_DIFF)

    def close(self) -> None:
        self.sa.close()
        self.sb.close()


def _nkey(n: str) -> str:
    return re.sub(r"[^0-9a-z]", "", n.lower())


def _snap_name(n: str, known: list[str]) -> str:
    """An OCR'd room name -> the other sheet's name it most likely is: equal letters / digits, or the same digits and
    >= 2/3 alike ('Yf 5' -> 'Yr 5', 'Ofice' -> 'Office'); else unchanged (a new room keeps its own name)."""
    import difflib

    keys = {_nkey(x): x for x in known}
    k = _nkey(n)
    if k in keys:
        return keys[k]
    dig = re.sub(r"\D", "", k)
    best = None
    for kk, x in keys.items():
        if re.sub(r"\D", "", kk) != dig or len(kk) < 2:
            continue
        r_ = difflib.SequenceMatcher(None, k, kk).ratio()
        if r_ >= 0.66 and (best is None or r_ > best[0]):
            best = (r_, x)
    return best[1] if best else n


def _number_same(a: Sheet, t: dict, same_fn) -> bool:
    """The tag's number looks clearly identical on both sheets: its own printed word(s) on a vector A (the digits of
    the value, inside the tag), else the whole tag line; every one must pass the strict pixel test."""
    tb = t.get("tag_box") or t["box"]
    digits = re.sub(r"\D", "", f"{t['value']:g}")
    boxes = [list(w[:4]) for w in a.words if re.sub(r"\D", "", w[4]) == digits and digits
             and w[0] >= tb[0] - 1 and w[2] <= tb[2] + 1 and w[1] >= tb[1] - 1 and w[3] <= tb[3] + 1]
    return all(same_fn(bx, strict=True) is True for bx in (boxes or [tb]))


def area_changes(a: Sheet, b: Sheet, M: np.ndarray, ocr: tuple[str, ...] = (), same_fn=None) -> dict:
    """Room tags compared by name (and place): per name the printed areas before / after, rooms added / removed.
    ocr: the sides ('a' / 'b') whose lines came from a scan's OCR layer: their room names are snapped to the other
    sheet's names first, and a value that differs only by OCR is checked against the pixels (same_fn(tag box in A) is
    True = the printed number looks the same on both sheets: OCR misread '80' as '60', not a change)."""
    ta, tb = area_tags(a), area_tags(b)
    if "b" in ocr and ta:
        tb = [{**t, "name": _snap_name(t["name"], [x["name"] for x in ta])} for t in tb]
    if "a" in ocr and tb:
        ta = [{**t, "name": _snap_name(t["name"], [x["name"] for x in tb])} for t in ta]
    if len(ta) < 2 and len(tb) < 2:
        return {"rooms": [], "by_name": [], "total": None}
    tbs = [(*_box_to_a(a, b, M, t["box"]), t) for t in tb]
    used: set[int] = set()
    fixed = [0]  # OCR readings the pixels overruled
    rooms = []
    diag = math.hypot(*a.native)
    for t in ta:
        c = ((t["box"][0] + t["box"][2]) / 2, (t["box"][1] + t["box"][3]) / 2)
        cands = [j for j, x in enumerate(tbs) if j not in used and x[4]["name"].lower() == t["name"].lower()
                 and x[4]["unit"] == t["unit"]]
        j = min(cands, key=lambda j: math.dist(((tbs[j][0] + tbs[j][2]) / 2, (tbs[j][1] + tbs[j][3]) / 2), c),
                default=None)
        if j is not None and math.dist(((tbs[j][0] + tbs[j][2]) / 2, (tbs[j][1] + tbs[j][3]) / 2), c) > 0.25 * diag:
            j = None
        if j is None:
            if "b" in ocr and same_fn is not None and _number_same(a, t, same_fn):
                # the OCR did not read this tag on the scan, but its pixels are clearly still there: not removed
                rooms.append({"name": t["name"], "old": t["value"], "new": t["value"], "unit": t["unit"],
                              "box_a": t["box"], "box_b": None, "kind": "same"})
                fixed[0] += 1
                continue
            rooms.append({"name": t["name"], "old": t["value"], "new": None, "unit": t["unit"], "box_a": t["box"],
                          "box_b": None, "kind": "removed"})
            continue
        used.add(j)
        tbj = tbs[j][4]
        kind = "same" if abs(tbj["value"] - t["value"]) < 1e-9 else "changed"
        cb = ((tbs[j][0] + tbs[j][2]) / 2, (tbs[j][1] + tbs[j][3]) / 2)
        in_place = math.dist(cb, c) <= 1.5 * max(t["box"][3] - t["box"][1], 1e-6)  # a moved tag: other pixels there
        if kind == "changed" and ocr and in_place and same_fn is not None and _number_same(a, t, same_fn):
            tbj = {**tbj, "value": t["value"]}  # the pixels say the number did not change
            kind = "same"
            fixed[0] += 1
        rooms.append({"name": t["name"], "old": t["value"], "new": tbj["value"], "unit": t["unit"],
                      "box_a": t["box"], "box_b": tbj["box"], "box_b_in_a": [round(v, 2) for v in tbs[j][:4]],
                      "kind": kind})
    for j, x in enumerate(tbs):
        if j not in used:
            rooms.append({"name": x[4]["name"], "old": None, "new": x[4]["value"], "unit": x[4]["unit"],
                          "box_a": None, "box_b": x[4]["box"], "box_b_in_a": [round(v, 2) for v in x[:4]],
                          "kind": "added"})
    by: dict[tuple, dict] = {}
    for r_ in rooms:
        k = (r_["name"].lower(), r_["unit"])
        d = by.setdefault(k, {"name": r_["name"], "unit": r_["unit"], "old": [], "new": []})
        if r_["old"] is not None:
            d["old"].append(r_["old"])
        if r_["new"] is not None:
            d["new"].append(r_["new"])
    by_name = []
    for d in by.values():
        so, sn = sum(d["old"]), sum(d["new"])
        if sorted(d["old"]) == sorted(d["new"]):
            continue
        # the rooms of this name whose printed area changed in place (Swiss sheets: 13 rooms named 'Zimmer', one changed)
        ch = [[r_["old"], r_["new"]] for r_ in rooms if r_["kind"] == "changed" and r_["name"].lower() == d["name"].lower()
              and r_["unit"] == d["unit"]]
        by_name.append({**d, "count_old": len(d["old"]), "count_new": len(d["new"]), "sum_old": round(so, 3),
                        "sum_new": round(sn, 3), "delta": round(sn - so, 3), "changed": ch})
    by_name.sort(key=lambda d: (not (d["count_old"] == d["count_new"] > 0), -abs(d["delta"])))  # same rooms first
    units = {r_["unit"] for r_ in rooms}
    total = None
    if len(units) == 1:
        so = sum(r_["old"] or 0 for r_ in rooms)
        sn = sum(r_["new"] or 0 for r_ in rooms)
        total = {"unit": units.pop(), "old": round(so, 3), "new": round(sn, 3), "delta": round(sn - so, 3),
                 "rooms_old": sum(1 for r_ in rooms if r_["old"] is not None),
                 "rooms_new": sum(1 for r_ in rooms if r_["new"] is not None)}
    out = {"rooms": [r_ for r_ in rooms if r_["kind"] != "same"], "by_name": by_name, "total": total}
    if fixed[0]:
        out["ocr_overruled"] = fixed[0]
    return out


def _ghost(self_g: np.ndarray, other_g: np.ndarray, pix: np.ndarray, box: tuple[slice, slice]) -> bool:
    """Is a mark that is ink on one sheet still FAINTLY there on the other (a thin line a scan made pale)? Averaged
    over the region's pixels, so noise cancels: the other sheet's darkening at those pixels (darkest within 1 px,
    against its own paper level measured the same way) is >= 30 % of this sheet's."""
    import cv2

    if pix.sum() < 8:
        return False
    k = np.ones((3, 3), np.uint8)
    so, oo = cv2.erode(self_g[box], k), cv2.erode(other_g[box], k)
    if GHOST_PAPER == "p90":  # paper = the brightest tenth of the box (dense hatching: the gaps between lines)
        c_self = float(np.percentile(self_g[box], 90)) - float(self_g[box][pix].mean())
        c_other = float(np.percentile(oo, 90)) - float(oo[pix].mean())  # both eroded: noise minima cancel
    else:
        c_self = float(np.median(so)) - float(self_g[box][pix].mean())
        c_other = float(np.median(oo)) - float(oo[pix].mean())
    return c_self > 0 and c_other >= GHOST * c_self


def door_changes(a: Sheet, b: Sheet, M: np.ndarray, scale_a: float | None) -> dict | None:
    """Door swings on both vector sheets (stoa.solvers.openings.find_swings, the openings reader's own arcs), matched
    through the alignment: same radius (±12 %) and centre within 0.15 m = unchanged; same radius within 3 m = moved;
    the rest removed (only on A) / added (only on B). Boxes = the swing's square (centre ± radius)."""
    if not (a.space == b.space == "pdf_pt" and a.kind == b.kind == "vector"):
        return None
    try:
        from stoa.evidence.linework import sheet_scales
        from stoa.solvers.openings import find_swings

        sb = sheet_scales(b.path, b.page or 0)
        arcs_a = find_swings(a.path, a.page or 0, scale_a)["arcs"]
        arcs_b = find_swings(b.path, b.page or 0, sb[0] if sb else scale_a)["arcs"]
    except Exception as e:  # noqa: BLE001  (a bonus reading: never lose the diff for it)
        return {"error": f"{type(e).__name__}: {e}"[:200]}
    m_pt = (scale_a or 100.0) * 0.0254 / 72  # metres per pt on sheet A (1:100 assumed when no scale is printed)

    def sq(c, r):
        return [round(float(c[0] - r), 2), round(float(c[1] - r), 2), round(float(c[0] + r), 2), round(float(c[1] + r), 2)]

    bb = []
    for x in arcs_b:
        box = _box_to_a(a, b, M, sq(x["c"], x["r"]))
        bb.append({"c": ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), "r": (box[2] - box[0]) / 2, "box_b": sq(x["c"], x["r"]),
                   "box_in_a": [round(v, 2) for v in box]})
    used: set[int] = set()
    pair_a, pair_b = _door_pairs(arcs_a), _door_pairs(arcs_b)
    out = {"count_old": len(arcs_a), "count_new": len(arcs_b), "unchanged": 0, "moved": [], "removed": [], "added": [],
           "sets_old": len(arcs_a) - len(pair_a) // 2, "sets_new": len(arcs_b) - len(pair_b) // 2,
           "source": "door swing arcs read from the vector paths of both sheets (openings reader)",
           "scale": f"1:{scale_a:g}" if scale_a else None}
    rest = []
    set_a = {id(x): min(i, pair_a.get(i, i)) for i, x in enumerate(arcs_a)}  # a double door's two leaves: one set id
    for j, y in enumerate(bb):
        y["_set"] = min(j, pair_b.get(j, j))
    for x in arcs_a:
        j = min((j for j, y in enumerate(bb) if j not in used and abs(y["r"] - x["r"]) <= 0.12 * x["r"]),
                key=lambda j: math.dist(bb[j]["c"], x["c"]), default=None)
        if j is not None and math.dist(bb[j]["c"], x["c"]) * m_pt <= 0.15:
            used.add(j)
            out["unchanged"] += 1
        else:
            rest.append(x)
    for x in rest:
        j = min((j for j, y in enumerate(bb) if j not in used and abs(y["r"] - x["r"]) <= 0.12 * x["r"]),
                key=lambda j: math.dist(bb[j]["c"], x["c"]), default=None)
        if j is not None and math.dist(bb[j]["c"], x["c"]) * m_pt <= 3.0:
            used.add(j)
            out["moved"].append({"box_a": sq(x["c"], x["r"]), "box_b": bb[j]["box_b"], "box_b_in_a": bb[j]["box_in_a"],
                                 "moved_m": round(float(math.dist(bb[j]["c"], x["c"]) * m_pt), 2)})
        else:
            out["removed"].append({"box_a": sq(x["c"], x["r"]), "set": set_a[id(x)]})
    for j, y in enumerate(bb):
        if j not in used:
            out["added"].append({"box_b": y["box_b"], "box_b_in_a": y["box_in_a"], "set": y["_set"]})
    out["sets_removed"] = len({d["set"] for d in out["removed"]})
    out["sets_added"] = len({d["set"] for d in out["added"]})
    return out


def _door_pairs(arcs: list[dict]) -> dict[int, int]:
    """Double doors among swing arcs: two leaves of the same width (±12 %) whose hinges are 1.8-2.2 leaf widths apart
    and whose closed leaves meet in the middle (an end of each arc within 0.25 r of the hinges' midpoint). DfE 630 FF:
    39 leaves = 5 pairs + 29 single = 34 door sets (= the pipeline's 'How many doors are there?' 34). {i: partner}."""
    cand = []
    for i, x in enumerate(arcs):
        for j in range(i + 1, len(arcs)):
            y = arcs[j]
            if abs(x["r"] - y["r"]) > 0.12 * x["r"]:
                continue
            d = math.dist(x["c"], y["c"]) / max(x["r"], 1e-9)
            if not 1.8 <= d <= 2.2:
                continue
            mid = ((x["c"][0] + y["c"][0]) / 2, (x["c"][1] + y["c"][1]) / 2)
            if all(min(math.dist(z["p0"], mid), math.dist(z["p1"], mid)) <= 0.25 * z["r"] for z in (x, y)):
                cand.append((abs(d - 2), i, j))
    out: dict[int, int] = {}
    for _, i, j in sorted(cand):
        if i not in out and j not in out:
            out[i], out[j] = j, i
    return out


def pixel_regions(a: Sheet, a_ink: np.ndarray, b_ink_w: np.ndarray, valid: np.ndarray, r: int,
                  min_px: int | None = None, a_weak: np.ndarray | None = None,
                  b_weak_w: np.ndarray | None = None, a_gray: np.ndarray | None = None,
                  b_gray_w: np.ndarray | None = None) -> tuple[list[dict], np.ndarray, np.ndarray]:
    """Removed = strong A ink farther than r px from any (weak) B ink; added = the other way round; grouped into
    regions."""
    import cv2

    a_weak = a_ink if a_weak is None else a_weak
    b_weak_w = b_ink_w if b_weak_w is None else b_weak_w
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    da = cv2.dilate(a_weak.astype(np.uint8), k) > 0
    db = cv2.dilate(b_weak_w.astype(np.uint8), k) > 0
    valid_a = valid & (cv2.erode(valid.astype(np.uint8), k) > 0)  # not at B's crop edge
    removed = a_ink & ~db & valid_a
    added = b_ink_w & ~da
    # a 1-px shimmer along a thick scanned line is not an edit
    o = np.ones((2, 2), np.uint8)
    if r >= 3:
        removed = cv2.morphologyEx(removed.astype(np.uint8), cv2.MORPH_OPEN, o) > 0
        added = cv2.morphologyEx(added.astype(np.uint8), cv2.MORPH_OPEN, o) > 0
    long_side = max(a.gray.shape)
    g = max(6, round(0.007 * long_side))  # pixels of one edit this close together = one region
    any_ch = (removed | added).astype(np.uint8)
    # long thin changed lines (a dimension / extension line whose ticks moved, a grid line) would chain separate
    # edits into one sheet-wide box: they are regions of their own and do not join others
    n0, lab0, st0, _ = cv2.connectedComponentsWithStats(any_ch, connectivity=8)
    thin_w = max(4, round(0.0015 * long_side))
    thin = np.zeros(n0, bool)
    for i in range(1, n0):
        w_, h_ = st0[i, 2], st0[i, 3]
        thin[i] = min(w_, h_) <= thin_w and max(w_, h_) >= max(12 * min(w_, h_), 4 * g)
    line_px = thin[lab0]
    core = any_ch.copy()
    core[line_px] = 0
    grown = cv2.dilate(core, cv2.getStructuringElement(cv2.MORPH_RECT, (2 * g + 1, 2 * g + 1)))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    lab = np.where(line_px, 0, lab).astype(np.int32)
    if line_px.any():  # thin lines group only with each other (a dimension line moved = its old + new copy)
        lgrown = cv2.dilate(line_px.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (2 * g + 1, 2 * g + 1)))
        nl, labl, statsl, _ = cv2.connectedComponentsWithStats(lgrown, connectivity=8)
        lab = np.where(line_px, labl + n - 1, lab).astype(np.int32)
        stats = np.vstack([stats, statsl[1:]])
        n += nl - 1
    min_px = min_px if min_px is not None else max(20, round((long_side / 3600) ** 2 * 40))
    regs = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        sl = (slice(y, y + h), slice(x, x + w))
        m = lab[sl] == i
        nr, na = int((removed[sl] & m).sum()), int((added[sl] & m).sum())
        if nr + na < min_px:
            continue
        if a_gray is not None and b_gray_w is not None:  # scans: drop marks the other sheet shows faintly
            pad = 6
            y0, y1 = max(0, y - pad), min(lab.shape[0], y + h + pad)
            x0, x1 = max(0, x - pad), min(lab.shape[1], x + w + pad)
            big = (slice(y0, y1), slice(x0, x1))
            mm = lab[big] == i
            if nr and _ghost(a_gray, b_gray_w, removed[big] & mm, big):
                removed[big] &= ~mm
                nr = 0
            if na and _ghost(b_gray_w, a_gray, added[big] & mm, big):
                added[big] &= ~mm
                na = 0
            if nr + na < min_px:
                continue
            # dust / toner specks: separate dot-sized blobs and no stroke at all (F5 noise pairs: 3 dots above a
            # title block were 'linework added 2.7 x 2.4 m' on 2 of 16 unchanged scans); a real edit draws lines
            if SPECK > 0:
                nc, _, stc, _ = cv2.connectedComponentsWithStats(((removed[sl] | added[sl]) & m).astype(np.uint8), 8)
                ext = stc[1:, 2:4]
                big_side, small_side = ext.max(axis=1), np.maximum(1, ext.min(axis=1))
                fill = stc[1:, 4] / np.maximum(1, ext[:, 0] * ext[:, 1])
                # round FILLED dots only (dust 5 x 5 px, fill 0.8-0.9); a deleted fitting's leftovers on a scan are
                # strokes (3 x 8) or hollow (fill 0.38) and stay a change (dfe_sen00 scans: 28 fittings deleted)
                if nc > 2 and (big_side <= SPECK * long_side).all() and (big_side <= 1.6 * small_side).all() \
                        and (fill >= 0.6).all():
                    continue
        ys, xs = np.nonzero((removed[sl] | added[sl]) & m)
        box = [float(x + xs.min()), float(y + ys.min()), float(x + xs.max() + 1), float(y + ys.max() + 1)]
        kind = "removed" if na < 0.15 * nr else "added" if nr < 0.15 * na else "changed"
        shift = None
        if nr and na:  # where the removed ink went: centroid of the added ink minus centroid of the removed ink
            yr, xr = np.nonzero(removed[sl] & m)
            yd, xd = np.nonzero(added[sl] & m)
            shift = [float(xd.mean() - xr.mean()), float(yd.mean() - yr.mean())]
        regs.append({"kind": kind, "px_removed": nr, "px_added": na, "box_px": box, "shift_px": shift})
    return regs, removed, added


def _fmt_q(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}".rstrip("0").rstrip(".")


def _nearest_label(a: Sheet, box_native: list[float], skip: set[str]) -> str | None:
    """The room-like text line nearest to a region (letters, not a dimension or a grid number)."""
    x0, y0, x1, y1 = box_native
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    reach = max(x1 - x0, y1 - y0) * 1.5 + (40 if a.space == "pdf_pt" else 120)
    best, bd = None, float("inf")
    for lx0, ly0, lx1, ly1, t in a.lines:
        letters = sum(c.isalpha() for c in t)
        if letters < 3 or letters < 0.5 * len(t.replace(" ", "")) or len(t) > 40 or t in skip:
            continue
        dx = max(lx0 - x1, x0 - lx1, 0)
        dy = max(ly0 - y1, y0 - ly1, 0)
        d = math.hypot(dx, dy) + 0.05 * math.hypot((lx0 + lx1) / 2 - cx, (ly0 + ly1) / 2 - cy)
        if d < bd and d <= reach:
            best, bd = t, d
    return best


def compare(path_a: str, path_b: str, page_a: int = 0, page_b: int | None = 0, virtual_a: str | None = None,
            virtual_b: str | None = None, out_dir: str | Path | None = None) -> dict:
    """Align B onto A and list the changes. Boxes come in both sheets' own evidence spaces."""
    import cv2

    t0 = time.perf_counter()
    a = load_sheet(path_a, page_a)
    page_note = None
    if page_b is None or page_b < 0:  # B's page that shares the most printed words with A (sets: the same floor)
        page_b, page_note = best_page(a, path_b)
    b = load_sheet(path_b, page_b)
    va, vb = virtual_a or path_a, virtual_b or path_b
    # the sheets' printed names: a PDF's at once (PyMuPDF is not thread-safe); a picture's title from its OCR text
    # layer (VP's sheet reader, ~3 s on a 14,000-px plan) beside the alignment
    names: dict = {k: sheet_name(pth, s_.page, v) for k, pth, s_, v in (("a", path_a, a, va), ("b", path_b, b, vb))
                   if s_.space == "pdf_pt"}
    namer = threading.Thread(target=lambda: names.update({k: sheet_name(pth, None, v) for k, pth, s_, v in
                                                          (("a", path_a, a, va), ("b", path_b, b, vb)) if k not in names}),
                             daemon=True)
    namer.start()
    exact = a.kind == "vector" and b.kind == "vector"
    a_ink, b_ink = ink_mask(a, "strong", exact), ink_mask(b, "strong", exact)
    a_weak, b_weak = (a_ink, b_ink) if exact else (ink_mask(a, "weak", exact), ink_mask(b, "weak", exact))
    al = align(a, b, a_ink, b_ink)
    res: dict = {"a": {"file": va, "page": a.page, "kind": a.kind, "space": a.space, "size": list(a.native),
                       "zoom": a.zoom},
                 "b": {"file": vb, "page": b.page, "kind": b.kind, "space": b.space, "size": list(b.native),
                       "zoom": b.zoom},
                 "alignment": al.describe(a), "regions": [], "text": [], "quantities": [], "summary": "",
                 "page_b_note": page_note}
    namer.join(timeout=20)
    res["a"]["name"] = names.get("a") or {"title": Path(va).stem, "number": None, "revision": None, "source": "file",
                                          "file": Path(va).name}
    res["b"]["name"] = names.get("b") or {"title": Path(vb).stem, "number": None, "revision": None, "source": "file",
                                          "file": Path(vb).name}
    if not al.ok:
        res["summary"] = ("Could not line the two sheets up (" + al.note + "). If they are two versions of the same "
                          "sheet, try the vector PDFs or cleaner scans; nothing is marked.")
        res["wall_seconds"] = round(time.perf_counter() - t0, 2)
        return res
    Minv = np.linalg.inv(al.M)
    if not exact:  # match the detail of the two sheets before the diff (a 1-px line on a fine render vs a coarse scan)
        sc = math.sqrt(abs(np.linalg.det(al.M[:2, :2])))  # A px per B px
        if sc > 1.15:
            a_ink, a_weak = ink_mask(a, "strong", False, 0.7 * sc), ink_mask(a, "weak", False, 0.7 * sc)
        elif sc < 1 / 1.15:
            b_ink, b_weak = ink_mask(b, "strong", False, 0.7 / sc), ink_mask(b, "weak", False, 0.7 / sc)
    b_ink_w = warp_to(a, b_ink.astype(np.uint8), al.M, fill=0, nearest=True) > 0
    b_weak_w = b_ink_w if exact else warp_to(a, b_weak.astype(np.uint8), al.M, fill=0, nearest=True) > 0
    valid = warp_to(a, np.ones(b_ink.shape, np.uint8), al.M, fill=0, nearest=True) > 0
    vector = exact
    r = int(np.clip(round(2 + 1.5 * min(al.residual_px, 4)), 2, 4)) if vector else int(
        np.clip(round(RASTER_R + al.residual_px), RASTER_R, 8))
    texts = text_changes(a, b, al.M)
    b_gray_w = None if exact else warp_to(a, b.gray, al.M)
    same_px: list = []  # label boxes (A render px) a scan check found unchanged
    res["text_check"] = None
    if not exact:  # a scan or a photo: labels are checked patch by patch and the different ones read by OCR
        rows, same_px, tinfo = scan_text_changes(a, b, al.M, slack=r + 2)
        texts += rows
        res["text_check"] = tinfo
    # text is compared as text (below), so its pixels do not make linework regions: mask every printed line's box
    # (both versions) and every label a scan check found unchanged out of the pixel diff
    a_ink_g, b_ink_g = a_ink, b_ink_w
    if texts or same_px:
        tmask = np.zeros(a_ink.shape, bool)
        pad = 0.6 * a.zoom ** 2 if exact else float(r)  # render px (vector: 0.6 zoom pt, as before)
        boxes_px = [[v * a.zoom for v in bx] for t in texts for bx in (t["box_a"], t["box_b_in_a"]) if bx] + same_px
        for bx in boxes_px:
            x0, y0 = max(0, int(bx[0] - pad)), max(0, int(bx[1] - pad))
            x1, y1 = int(math.ceil(bx[2] + pad)), int(math.ceil(bx[3] + pad))
            tmask[y0:y1, x0:x1] = True
        a_ink_g, b_ink_g = a_ink & ~tmask, b_ink_w & ~tmask
    grays = {} if exact else {"a_gray": a.gray, "b_gray_w": b_gray_w}
    res["blur_match"] = None
    if not exact and BLUR_MATCH:
        # the ghost test compares a mark's contrast on both sheets: at the SAME blur (a hairline a phone photo smeared
        # keeps ~20 % of its contrast on the photo, and would keep the same on the drawing blurred alike)
        import cv2

        gap = _blur_gap(a, b_gray_w, valid)
        if gap > 0:
            grays["a_gray"] = cv2.GaussianBlur(a.gray, (0, 0), gap)
        elif gap < 0:
            grays["b_gray_w"] = cv2.GaussianBlur(b_gray_w, (0, 0), -gap)
        res["blur_match"] = {"sigma_px": gap}
    regs, removed, added = pixel_regions(a, a_ink_g, b_ink_g, valid, r, a_weak=a_weak, b_weak_w=b_weak_w, **grays)
    # edits inside hatching / siding, which the tolerance diff cannot see: scan vs scan only (8 synthetic pairs: on CAD vs
    # scan / photo it added 2 false marks in dense title-block text, which the per-word label check covers there)
    if not exact and DENSE and a.kind != "vector" and b.kind != "vector":
        taken = [rg["box_px"] for rg in regs] + [[v * a.zoom for v in bx] for t in texts
                                                  for bx in (t["box_a"], t["box_b_in_a"]) if bx]
        regs += dense_regions(a_ink, b_ink_w, valid, grays["a_gray"], grays["b_gray_w"], r + 2, taken)
    ink_total = max(1, int((a_ink & valid).sum()))
    changed_share = (int(removed.sum()) + int(added.sum())) / ink_total

    def to_native_a(bp):
        return [bp[0] / a.zoom, bp[1] / a.zoom, bp[2] / a.zoom, bp[3] / a.zoom]

    def to_native_b(bp):
        pts = np.float32([[bp[0], bp[1]], [bp[2], bp[1]], [bp[2], bp[3]], [bp[0], bp[3]]])
        q = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), Minv).reshape(-1, 2) / b.zoom
        return [float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max())]

    # text pairs give direct quantity deltas ('A: 62 m2' -> 'A: 55 m2')
    quants = []
    for t in texts:
        if t["kind"] != "changed":
            continue
        qa, qb = _quantity(t["old"] or ""), _quantity(t["new"] or "")
        if qa and qb and qa[1] == qb[1] and qa[0] != qb[0]:
            quants.append({"what": t["old"], "old": qa[0], "new": qb[0], "unit": qa[1],
                           "delta": round(qb[0] - qa[0], 4), "box_a": t["box_a"], "box_b": t["box_b"],
                           "source": "printed text on both sheets"})
    skip = {t["old"] for t in texts if t["old"]} | {t["new"] for t in texts if t["new"]}
    m_per_unit, scale_txt = None, None
    if a.space == "pdf_pt":  # the printed scale of sheet A ('1:200@A3' on A3 -> 200): sizes in metres
        try:
            from stoa.evidence.linework import sheet_scales

            sc = sheet_scales(a.path, a.page or 0)
            if sc:
                m_per_unit, scale_txt = sc[0] * 0.0254 / 72, f"1:{sc[0]:g}"
        except Exception:  # noqa: BLE001
            pass
    res["scale"] = {"ratio": scale_txt, "source": "printed on sheet A"} if scale_txt else None

    def size_txt(w_n, h_n):
        if m_per_unit:
            return f"{w_n * m_per_unit:.1f} × {h_n * m_per_unit:.1f} m"
        return f"{w_n * 25.4 / 72:.0f} × {h_n * 25.4 / 72:.0f} mm on paper" if a.space == "pdf_pt" else \
            f"{w_n:.0f} × {h_n:.0f} px"

    for i, rg in enumerate(sorted(regs, key=lambda r_: -(r_["px_removed"] + r_["px_added"]))):
        bp = rg["box_px"]
        box_a = to_native_a(bp)
        near = _nearest_label(a, box_a, set()) if a.lines else None
        w_n, h_n = box_a[2] - box_a[0], box_a[3] - box_a[1]
        verb = {"added": "added", "removed": "removed", "changed": "changed"}[rg["kind"]]
        desc = f"linework {verb} ({size_txt(w_n, h_n)})" if not rg.get("dense") else \
            f"dense linework / hatching changed ({size_txt(w_n, h_n)})"
        moved = None
        g_px = max(6, round(0.007 * max(a.gray.shape)))  # pixel_regions' grouping distance
        one_thing = (rg["kind"] == "changed" and rg.get("shift_px") and 0.5 <= rg["px_added"] / max(1, rg["px_removed"]) <= 2
                     and math.hypot(*rg["shift_px"]) <= 3 * g_px)  # same ink, shifted: one element moved
        if one_thing and m_per_unit and min(w_n, h_n) * m_per_unit <= 4:
            dx, dy = (v / a.zoom * m_per_unit for v in rg["shift_px"])
            if math.hypot(dx, dy) >= 0.1:
                big = max((abs(dx), "right" if dx > 0 else "left"), (abs(dy), "down" if dy > 0 else "up"))
                moved = round(math.hypot(dx, dy), 2)
                desc = f"linework moved ≈ {moved:.1f} m ({big[1]} on the sheet; {size_txt(w_n, h_n)})"
        desc += f", near '{near}'" if near else ""
        res["regions"].append({
            "id": i + 1, "kind": rg["kind"], "what": "linework", "description": desc, "near": near,
            "box_a": [round(v, 2) for v in box_a], "box_b": [round(v, 2) for v in to_native_b(bp)],
            "px_removed": rg["px_removed"], "px_added": rg["px_added"], "moved_m": moved,
            "size_m": [round(w_n * m_per_unit, 2), round(h_n * m_per_unit, 2)] if m_per_unit else None,
            # scans / photos: the run's length from its own ink (the box can run on along a wall it joins)
            "run_m": _ink_run_m(added if rg["kind"] == "added" else removed, bp, m_per_unit / a.zoom)
            if (not exact and m_per_unit and rg["kind"] in ("added", "removed")) else None,
        })
    if exact and m_per_unit and res["regions"] and (WALL_MEASURE or HATCH_EDGE):
        try:
            b_w = warp_to(a, b.gray, al.M)
            if HATCH_EDGE:
                _mark_hatch_edges(a, b_w, res["regions"], m_per_unit)
            if WALL_MEASURE:
                _wall_lengths(a, b_w, valid, res["regions"], m_per_unit)
        except Exception:  # noqa: BLE001  (a bonus reading for the cost lines: never lose the diff for it)
            pass
    res["text"] = [{k: t.get(k) for k in ("kind", "old", "new", "box_a", "box_b", "box_b_in_a", "shift", "source")} for t in texts]
    res["quantities"] = quants
    ocr_sides, pending = [], []
    for side, s_ in (("a", a), ("b", b)):  # scans: room tags from the upload's OCR layer (after the text diff above)
        if not s_.lines and s_.space == "image_px" and OCR_AREAS:
            got = layer_lines(s_)
            if got:
                s_.lines = got
                ocr_sides.append(side)
            elif time.time() - Path(s_.path).stat().st_mtime < LAYER_WAIT_S:
                pending.append(side)  # just uploaded: the warm-up is probably still reading it
    pt = PatchTest(a, b, al.M, r + 2) if ocr_sides else None
    try:
        res["areas"] = area_changes(a, b, al.M, tuple(ocr_sides), pt) if (a.lines and b.lines) else \
            {"rooms": [], "by_name": [], "total": None}
    finally:
        if pt is not None:
            pt.close()
    if ocr_sides and res["areas"]["by_name"]:
        res["areas"]["source"] = "room tags read from the scan by OCR: check the numbers on the drawing"
    res["areas_pending"] = bool(pending) and not (a.lines and b.lines)
    res["doors"] = door_changes(a, b, al.M, float(scale_txt[2:]) if scale_txt else None) if exact else None
    res["changed_share"] = round(changed_share, 4)
    res["same_sheet"] = bool(al.overlap >= 0.8 and changed_share <= 0.25)
    # two DIFFERENT drawings (another floor / building / discipline) that still line up somewhat: never priced.
    # Measured: 22 same-sheet scans / photos / exports 0.92-0.99, the real revisions 0.93-0.99 (DfE 2014 FF 0.935,
    # 2012 FF 0.926); other floors of one school 0.62-0.78, scans of another floor 0.68-0.70 (cross_pairs.py)
    res["other_sheet"] = bool(al.overlap < OTHER_SHEET)
    res["drawing_kind"] = {"a": drawing_kind(a), "b": drawing_kind(b)}
    res["approximate"] = not exact  # scans / photos: marks are pixel differences, drawn dashed in the UI
    res["summary"] = summarize(res, al)
    res["suggested_questions"] = suggest_questions(res, skip)
    res["revision_cost"] = revision_cost(res)  # floor, doors, walls now; skirting (traced rooms) via /cost
    if out_dir is not None:
        write_images(a, b, al.M, removed, added, Path(out_dir), texts)
    res["wall_seconds"] = round(time.perf_counter() - t0, 2)
    return res


def _ink_run_m(mask: np.ndarray, box_px: list, m_per_px: float) -> float | None:
    """Length of a thin run of changed ink on a scan / photo, from the ink itself: along the box's long side, count the
    columns where the ink is at least half the run's typical thickness (bits < 3 px dropped), pieces joined across gaps
    up to 1.2 m, the longest chain's span.
    The box alone runs on along a wall the new one joins: the DfE L00 scan's new wall (true 6.49 m) measured 7.0 m
    upright but 8.7 m when the same scan was turned 270° (a 1-2 px residual along the joining wall)."""
    try:
        x0, y0, x1, y1 = (int(round(v)) for v in box_px)
        crop = mask[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        if crop.size == 0:
            return None
        horiz = crop.shape[1] >= crop.shape[0]
        prof = crop.sum(axis=0 if horiz else 1).astype(float)
        on = prof[prof > 0]
        if len(on) < 3:
            return None
        solid = np.flatnonzero(prof >= max(2.0, 0.5 * float(np.percentile(on, 75))))
        if not len(solid):
            return None
        segs = [g for g in np.split(solid, np.flatnonzero(np.diff(solid) > 3) + 1) if len(g) >= 3]
        if not segs:
            return None
        # pieces of one wall: the new wall crosses existing lines (no NEW ink there: DfE L00 scan gaps 0.26 / 0.5 m)
        # or a door opening (<= 1.2 m); a piece further away is something else (the tail along a joining wall)
        join = 1.2 / m_per_px
        chains: list[list] = [[segs[0]]]
        for g in segs[1:]:
            if g[0] - chains[-1][-1][-1] <= join:
                chains[-1].append(g)
            else:
                chains.append([g])
        best = max(chains, key=lambda c: sum(len(g) for g in c))
        return round(int(best[-1][-1] - best[0][0] + 1) * m_per_px, 2)
    except Exception:  # noqa: BLE001
        return None


OTHER_SHEET = float(os.environ.get("STOA_F5_OTHER", "0.85"))
UPRIGHT = os.environ.get("STOA_F5_UPRIGHT", "1") != "0"  # read room tags of a turned re-plot on an upright copy  # overlap below this = two different drawings: no cost
_VIEW3D = re.compile(r"\b(?:axo(?:nometric)?|isometric|exploded|3d)\b", re.I)


def drawing_kind(s: Sheet) -> str | None:
    """'plan' / 'elev' (a section or an elevation, also a 3D / axonometric view) from the sheet's printed title lines
    (stoa.formats.pages._sheet_kind on the text lines; images: their OCR'd lines when the upload read them), else None.
    DfE 2012 420-place sections and 3D sections -> 'elev' (their compare priced section lines as partitions: +EUR 13,489)."""
    try:
        from stoa.formats.pages import _sheet_kind

        lines = [t for *_, t in (s.lines or [])] or [t for *_, t in (layer_lines(s) or [])]
        text = "\n".join(lines)
        k = _sheet_kind(text)
        if k is None and any(len(t) <= 60 and _VIEW3D.search(t) for t in lines):
            k = "elev"
        return k
    except Exception:  # noqa: BLE001
        return None


WALL_MEASURE = os.environ.get("STOA_F5_WALLS", "1") != "0"
HATCH_EDGE = os.environ.get("STOA_F5_HATCH_EDGE", "1") != "0"


def _mark_hatch_edges(a: Sheet, b_w: np.ndarray, regions: list[dict], m_per_unit: float) -> None:
    """A thin run with REPEATED lines right beside it (a 0.6 m strip with >= 15 % ink crossing >= 3 separate lines) is
    the edge of a hatched area (a roof block, paving, a slab), not a partition: r['beside_hatch']. DfE 2012 420-place
    second floor: an 8.7 m roof-block edge was priced as a removed wall (4 lines beside it); real partitions and the
    DfE 2014 window filled in next to the solid outer wall cross <= 2 (vector pairs, F5 cycle 5)."""
    ppm = a.zoom / m_per_unit  # render px per metre
    strip, gap = max(4, int(0.6 * ppm)), max(2, int(0.08 * ppm))
    for r_ in regions:
        sz = r_.get("size_m")
        if not sz or r_["kind"] not in ("added", "removed") or not (min(sz) <= WALL_THIN_M and max(sz) >= WALL_MIN_M):
            continue
        g = a.gray if r_["kind"] == "removed" else b_w
        x0, y0, x1, y1 = (int(round(v * a.zoom)) for v in r_["box_a"])
        if (x1 - x0) >= (y1 - y0):  # strips above and below; rows (axis 0) cross the strip
            sides = (g[max(0, y0 - gap - strip):max(0, y0 - gap), x0:x1], g[y1 + gap:y1 + gap + strip, x0:x1])
        else:
            sides = (g[y0:y1, max(0, x0 - gap - strip):max(0, x0 - gap)].T, g[y0:y1, x1 + gap:x1 + gap + strip].T)
        for s_ in sides:
            if s_.size == 0:
                continue
            ink = s_ < 160
            if ink.mean() < 0.15:
                continue
            crossed = (np.diff(ink.astype(np.int8), axis=0) == 1).sum(axis=0) + ink[0]
            if float(np.median(crossed)) >= 3:
                r_["beside_hatch"] = True
                break


def _wall_ink(g: np.ndarray) -> np.ndarray:
    """Per pixel: 1 / the local width of THICK dark ink (walls drawn solid; thin lines, text, hatching, door leaves
    opened away), so the sum over a wall = its length in px. Width = 2 x the distance-transform ridge within 15 px."""
    import cv2

    thick = cv2.morphologyEx((g < 110).astype(np.uint8), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    dt = cv2.distanceTransform(thick, cv2.DIST_L2, 3)
    ridge = cv2.dilate(dt, np.ones((15, 15), np.uint8))
    return np.where(thick > 0, 1.0 / np.maximum(2.0 * ridge, 1.0), 0.0).astype(np.float32)


def _wall_lengths(a: Sheet, b_gray_w: np.ndarray, valid: np.ndarray, regions: list[dict], m_per_unit: float) -> None:
    """Wall length inside each changed region on both vector sheets ('walls_m' {old, new}): the thick-ink length map of
    each version summed over the region's box padded by 14 px (a strip that filled a window in a thick outer wall counts
    the wall's whole width). Each pixel belongs to ONE region (moved runs first, then by size), so overlapping boxes do
    not count a change twice. DfE 630 FF: the new partition 7.1 m (box 7.09), a window filled in 1.35 m (1.2), the
    classrooms' moved partitions +0.65 m (same length); about ±15 % per region: approximate."""
    ca, cb = _wall_ink(a.gray), _wall_ink(b_gray_w)
    cb = np.where(valid, cb, ca)
    taken = np.zeros(ca.shape, bool)
    k = m_per_unit / a.zoom  # metres per render px
    order = sorted(regions, key=lambda r_: (not r_.get("moved_m"), -((r_["box_a"][2] - r_["box_a"][0]) * (r_["box_a"][3] - r_["box_a"][1]))))
    H, W = ca.shape
    for r_ in order:
        x0, y0, x1, y1 = (v * a.zoom for v in r_["box_a"])
        sl = (slice(max(0, int(y0) - 14), min(H, int(math.ceil(y1)) + 14)), slice(max(0, int(x0) - 14), min(W, int(math.ceil(x1)) + 14)))
        free = ~taken[sl]
        la, lb = float(ca[sl][free].sum()) * k, float(cb[sl][free].sum()) * k
        taken[sl] = True
        r_["walls_m"] = {"old": round(la, 2), "new": round(lb, 2)}


def summarize(res: dict, al: Alignment) -> str:
    regs, texts = res["regions"], res["text"]
    n = collections.Counter(r_["kind"] for r_ in regs)
    nt = collections.Counter(t["kind"] for t in texts)
    parts = []
    if regs:
        parts.append(f"{_n(len(regs), 'linework change')} ({n['added']} added, {n['removed']} removed, "
                     f"{n['changed']} changed)")
    if texts:
        parts.append(", ".join(f"{_n(v, 'label')} {k}" for k, v in nt.most_common()))
    head = "; ".join(parts) if parts else "No differences found"
    out = f"{head} (lined up {res['alignment'].get('how', al.method)}, {res['alignment']['overlap']:.0%} of the linework matches)."
    dr = res.get("doors") or {}
    if dr.get("count_old") is not None and (dr["moved"] or dr["added"] or dr["removed"]):
        out += (f" Door leaves (swing arcs) {dr['count_old']} → {dr['count_new']}: {len(dr['added'])} added, {len(dr['removed'])} "
                f"removed, {len(dr['moved'])} moved.")
    ar = res.get("areas") or {}
    if ar.get("by_name"):
        lines = []
        for d in ar["by_name"][:4]:
            u = "m²" if d["unit"] == "m2" else "ft²"
            if d["count_old"] == d["count_new"] and len(set(d["old"])) == 1 and len(set(d["new"])) == 1:
                lines.append(f"{d['name']}: {_fmt_q(d['old'][0])} → {_fmt_q(d['new'][0])} {u}"
                             + (f" (×{d['count_old']})" if d["count_old"] > 1 else ""))
            elif _partial(d, u):
                lines.append(f"{d['name']}: {_partial(d, u)}")
            else:
                lines.append(f"{d['name']}: {_n(d['count_old'], 'room')} {_fmt_q(d['sum_old'])} {u} → "
                             f"{_n(d['count_new'], 'room')} {_fmt_q(d['sum_new'])} {u}")
        out += " Printed room areas: " + "; ".join(lines) + "."
        tot = ar.get("total")
        if tot and tot["delta"]:
            u = "m²" if tot["unit"] == "m2" else "ft²"
            out += f" Sum of all printed room areas {_fmt_q(tot['old'])} → {_fmt_q(tot['new'])} {u} ({tot['delta']:+,.1f})."
    if res.get("other_sheet"):
        out += (f" Only {res['alignment']['overlap']:.0%} of the linework lines up: these look like two different drawings (another "
                "floor or sheet?), not two versions of one; the marks show where they differ, nothing is priced.")
    elif not res.get("same_sheet", True):
        out += (f" {res['changed_share']:.0%} of the linework differs and {res['alignment']['overlap']:.0%} lines up: these may be "
                "different sheets (another floor?) or a poor scan; treat the marks with care.")
    return out


def write_images(a: Sheet, b: Sheet, M: np.ndarray, removed: np.ndarray, added: np.ndarray, out: Path,
                 texts: list[dict] | None = None) -> None:
    """overlay.png: A light grey, removed red, added green (the convention of drawing-compare tools);
    b_aligned.png: B warped onto A's frame (same size as A's render) for a swipe / flicker view."""
    import cv2

    out.mkdir(parents=True, exist_ok=True)
    base = (255 - (255 - a.gray) * 0.35).astype(np.uint8)
    bw = warp_to(a, b.gray, M)
    base = np.minimum(base, (255 - (255 - bw) * 0.35).astype(np.uint8))
    img = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    k = np.ones((3, 3), np.uint8)
    rm = cv2.dilate(removed.astype(np.uint8), k) > 0
    ad = cv2.dilate(added.astype(np.uint8), k) > 0
    img[rm] = (40, 40, 220)
    img[ad] = (60, 170, 30)
    colours = {"removed": (40, 40, 220), "added": (60, 170, 30), "changed": (0, 140, 255), "moved": (200, 120, 0)}
    for t in texts or []:
        for bx, side in ((t["box_a"], "a"), (t["box_b_in_a"], "b")):
            if not bx or (t["kind"] == "moved" and side == "a"):
                continue
            p0 = (int(bx[0] * a.zoom) - 3, int(bx[1] * a.zoom) - 3)
            p1 = (int(bx[2] * a.zoom) + 3, int(bx[3] * a.zoom) + 3)
            cv2.rectangle(img, p0, p1, colours[t["kind"]], 2)
    cv2.imwrite(str(out / "overlay.png"), img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    cv2.imwrite(str(out / "b_aligned.png"), bw, [cv2.IMWRITE_PNG_COMPRESSION, 3])
    cv2.imwrite(str(out / "a.png"), a.gray, [cv2.IMWRITE_PNG_COMPRESSION, 3])


# ------------------------------------------------------------------------------------------------------ impact
_ROOM_STOP = re.compile(r"\b(plan|level|floor|scale|drawing|dwg|rev|date|title|notes?|key|section|elevation|"
                        r"project|client|status|sheet|north|copyright|issue)\b", re.I)


# ------------------------------------------------------------------------ the cost of a revision (captain 21:51)
# Estimators turn quantity changes into money and orders (artifacts/RESEARCH/VERIFIED.md): every line here is a NET
# quantity change between the two versions × an EXAMPLE unit price the visitor overwrites in the panel (never shown as
# a real price). What we could not measure is listed for a person's check and kept OUT of the total.
PRICES = [
    {"key": "door", "label": "Door set (leaf, frame, ironmongery)", "unit": "door", "eur": 450.0},
    {"key": "skirting", "label": "Skirting", "unit": "m", "eur": 12.0},
    {"key": "floor", "label": "Floor covering", "unit": "m²", "eur": 38.0},
    {"key": "floor_wet", "label": "Floor tiling (wet rooms)", "unit": "m²", "eur": 65.0},
    {"key": "partition", "label": "Partition wall", "unit": "m²", "eur": 95.0},
]
WALL_H = float(os.environ.get("STOA_F5_WALL_H", "2.7"))  # m: partition height, an assumption the panel lets you change
TRACE_TOL = 0.08  # a traced room outline is used when its area is within 8 % of the room's printed area
WALL_THIN_M, WALL_MIN_M = 0.45, 0.5  # a linework region this thin and at least this long = one wall run
SKIRT_MAX_ROOMS = 40  # rooms traced per compare (2 traces each, ~0.5 s per trace)
FT2 = 0.09290304


def _wet(name: str) -> bool:
    """F2's room types (stoa.features.takeoff.room_type): a wet room is tiled and gets no skirting."""
    try:
        from stoa.features.takeoff import room_type

        return room_type(name) == "wet"
    except Exception:  # noqa: BLE001
        return bool(re.search(r"toilet|\bwc\b|shower|bath|changing|laundry|cleaner", name, re.I))


def _no_skirting(name: str, wet: bool = False) -> bool:
    """Rooms without skirting: stairs / lifts / voids (lineplan's rule) and, unless `wet` (F4's Conventions tab 'wet
    rooms included'), tiled wet rooms."""
    if not wet and _wet(name):
        return True
    try:
        from stoa.evidence import lineplan

        return bool(lineplan._NO_SKIRTING.search(name))
    except Exception:  # noqa: BLE001
        return False


def _room_marks(rooms: list[dict], name: str) -> list[dict]:
    out = []
    for r_ in rooms:
        if r_["name"].lower() != name.lower():
            continue
        if r_.get("box_a"):
            out.append({"box": r_["box_a"], "kind": "removed" if r_["kind"] == "removed" else "changed"})
        if r_.get("box_b_in_a") and r_["kind"] == "added":
            out.append({"box": r_["box_b_in_a"], "kind": "added"})
    return out


def _partial(d: dict, u: str) -> str | None:
    """'12.09 → 10.5 m² (1 of 13 rooms)': some of the rooms sharing a name changed, the others did not."""
    ch = d.get("changed") or []
    if d["count_old"] != d["count_new"] or not ch or len(ch) >= d["count_old"]:
        return None
    return (", ".join(f"{_fmt_q(o)} → {_fmt_q(n)}" for o, n in ch[:3]) + (", …" if len(ch) > 3 else "")
            + f" {u} ({len(ch)} of {d['count_old']} rooms)")


def _each(vals: list[float], u: str) -> str:
    return " + ".join(_fmt_q(v) for v in vals) + f" {u}"


def best_gap(x: dict, y: dict, m_unit: float | None) -> float:
    """Distance between two boxes' centres in metres (sheet A units × m_unit)."""
    cx = ((x["box_a"][0] + x["box_a"][2]) / 2, (x["box_a"][1] + x["box_a"][3]) / 2)
    cy = ((y["box_a"][0] + y["box_a"][2]) / 2, (y["box_a"][1] + y["box_a"][3]) / 2)
    return math.dist(cx, cy) * (m_unit or 0.0)


def withheld(res: dict) -> str | None:
    """Why this compare gets no cost (None = priced): two different drawings that line up only partly (another floor
    of the same school: -EUR 30,556 .. -EUR 68,797 'saved'), or a section / elevation / 3D view (its long lines are
    floors, roofs and facades, not partitions)."""
    kinds = res.get("drawing_kind") or {}
    if "elev" in (kinds.get("a"), kinds.get("b")):
        return ("this is a section or an elevation: its lines are floors, roofs and facades, not partitions or doors. "
                "Compare the two floor plans for a cost")
    if res.get("other_sheet"):
        ov = (res.get("alignment") or {}).get("overlap") or 0.0
        return (f"these look like two different drawings (only {ov:.0%} of the linework lines up: another floor, "
                "building or sheet?), so a cost would compare two drawings, not two versions of one")
    return None


def revision_cost(res: dict, skirting: dict | None = None) -> dict:
    """The quantity changes between the two versions as priced lines (example prices; the panel recomputes with the
    visitor's own): floor finishes from the printed room areas, door SETS from the swing arcs (a double door = 1 set),
    partitions from wall runs that appear / vanish (× WALL_H), skirting from each changed room's traced outline minus
    its openings (`skirting`, from skirting_changes: slower, so the panel fetches it from /cost). 'review' = things
    seen but not measured: never in the total."""
    why = withheld(res)
    if why:  # two different drawings, or a section / elevation: a total would be a made-up number
        return {"prices": PRICES, "wall_height_m": WALL_H, "lines": [], "total_eur": 0.0, "skirting": "n/a",
                "review": [{"id": "review:withheld", "item": "Not priced", "why": why, "marks": []}],
                "currency": "EUR", "withheld": why, "rule": "only two versions of the same floor plan are priced",
                "prices_note": "example prices — use your own", "summary": f"Not priced: {why}."}
    lines: list[dict] = []
    review: list[dict] = []
    ar = res.get("areas") or {}
    rooms = ar.get("rooms") or []
    ocr = bool(ar.get("source"))
    for d in ar.get("by_name") or []:
        if d["unit"] not in ("m2", "ft2"):
            continue
        k, u = (1.0, "m²") if d["unit"] == "m2" else (FT2, "m²")
        uniform = d["count_old"] == d["count_new"] and len(set(d["old"])) == 1 and len(set(d["new"])) == 1
        unit_txt = "m²" if d["unit"] == "m2" else "ft²"
        detail = (f"{_fmt_q(d['old'][0])} → {_fmt_q(d['new'][0])} {unit_txt}" + (f" each, {d['count_old']} rooms" if d["count_old"] > 1 else "")
                  if uniform else f"new room: {_each(d['new'], unit_txt)}" if not d["count_old"]
                  else f"room gone: {_each(d['old'], unit_txt)}" if not d["count_new"]
                  else _partial(d, unit_txt) or f"{_each(d['old'], unit_txt)} → {_each(d['new'], unit_txt)}")
        lines.append({"id": f"floor:{d['name']}", "group": "floor", "item": d["name"], "detail": detail,
                      "old": round(d["sum_old"] * k, 2), "new": round(d["sum_new"] * k, 2), "delta": round(d["delta"] * k, 2),
                      "unit": u, "price": "floor_wet" if _wet(d["name"]) else "floor", "approx": ocr,
                      "how": "printed room areas on both sheets" + (" (read from the scan by OCR: check)" if ocr else ""),
                      "marks": _room_marks(rooms, d["name"])})
    dr = res.get("doors")
    if dr and dr.get("count_old") is not None:
        sa = dr.get("sets_added", len(dr["added"]))
        sr = dr.get("sets_removed", len(dr["removed"]))
        how = "door swings drawn on the sheets (no model); a double door = 1 set"
        if sa:
            lines.append({"id": "doors:added", "group": "doors", "item": "New doors", "delta": sa, "unit": "door",
                          "price": "door", "how": how, "detail": f"{_n(len(dr['added']), 'leaf', 'leaves')} = {_n(sa, 'door set')}, only on the new version",
                          "marks": [{"box": x["box_b_in_a"], "kind": "added"} for x in dr["added"]]})
        if sr:
            lines.append({"id": "doors:removed", "group": "doors", "item": "Doors taken out", "delta": -sr, "unit": "door",
                          "price": "door", "how": how, "detail": f"{_n(len(dr['removed']), 'leaf', 'leaves')} = {_n(sr, 'door set')}, only on the old version",
                          "marks": [{"box": x["box_a"], "kind": "removed"} for x in dr["removed"]]})
        if dr["moved"]:
            lines.append({"id": "doors:moved", "group": "doors", "item": "Doors moved", "delta": 0, "unit": "door",
                          "price": None, "how": how, "detail": f"{len(dr['moved'])} leaves at a new place: the same doors, "
                          "no quantity change (price the move as rework if already built)",
                          "marks": [{"box": x["box_a"], "kind": "changed"} for x in dr["moved"]]})
    elif res.get("approximate") and res.get("regions"):  # nothing changed on the scan: no door changed either
        review.append({"id": "review:doors", "item": "Doors", "why": "door swings are compared on vector PDFs only: "
                       "check the marked changes on the scan for doors by hand",
                       "marks": [{"box": r_["box_a"], "kind": r_["kind"]} for r_ in res["regions"][:12]]})
    moved, unmeasured, walls_on_scan, runs, edges = [], [], [], [], []
    approx = bool(res.get("approximate"))
    door_boxes = [] if not dr or dr.get("count_old") is None else (
        [x["box_a"] for x in dr["removed"]] + [x["box_b_in_a"] for x in dr["added"]]
        + [x[k] for x in dr["moved"] for k in ("box_a", "box_b_in_a") if x.get(k)])

    def on_door(box) -> bool:  # the linework of a door priced in the Doors lines (leaf + swing)
        area = max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))
        for d in door_boxes:
            g = 0.15 * (d[2] - d[0])
            iw = min(box[2], d[2] + g) - max(box[0], d[0] - g)
            ih = min(box[3], d[3] + g) - max(box[1], d[1] - g)
            if iw > 0 and ih > 0 and iw * ih >= 0.6 * area:
                return True
        return False

    for r_ in res.get("regions") or []:
        if door_boxes and on_door(r_["box_a"]):
            continue
        sz = r_.get("size_m")
        if not sz:
            walls_on_scan.append(r_)
            continue
        long_, thin = max(sz), min(sz)
        if r_.get("moved_m"):
            moved.append(r_)
        elif r_.get("beside_hatch"):
            edges.append(r_)
        elif r_["kind"] in ("added", "removed") and (
                (thin <= WALL_THIN_M and long_ >= WALL_MIN_M) if not approx
                # scans / photos: marks are padded by the pixel tolerance and blur (the DfE L00 scan's new wall boxes
                # 7.4 x 0.6 m), and short blobs there are mostly noise: thin for its length and >= 1.5 m long
                else (thin <= max(WALL_THIN_M, 0.15 * long_) and long_ >= 1.5)):
            runs.append(r_)
        else:
            unmeasured.append(r_)
    # a run that vanished + a parallel run of about the same length that appeared within 3 m = ONE wall moved (the
    # tolerance diff sees a move of more than a few px as two runs): priced only by its change in length
    m_unit = None
    for r_ in runs:
        w_n = r_["box_a"][2] - r_["box_a"][0]
        if w_n > 0 and r_["size_m"][0] > 0:
            m_unit = r_["size_m"][0] / w_n
            break
    rem = [r_ for r_ in runs if r_["kind"] == "removed"]
    pairs: dict[int, dict] = {}
    for x in [r_ for r_ in runs if r_["kind"] == "added"]:
        hx = x["size_m"][0] >= x["size_m"][1]
        best = None
        for y in rem:
            if id(y) in {id(v) for v in pairs.values()} or (y["size_m"][0] >= y["size_m"][1]) != hx:
                continue
            lx, ly = max(x["size_m"]), max(y["size_m"])
            if abs(lx - ly) > 0.15 * max(lx, ly):
                continue
            cx = ((x["box_a"][0] + x["box_a"][2]) / 2, (x["box_a"][1] + x["box_a"][3]) / 2)
            cy = ((y["box_a"][0] + y["box_a"][2]) / 2, (y["box_a"][1] + y["box_a"][3]) / 2)
            across, along = (abs(cx[1] - cy[1]), abs(cx[0] - cy[0])) if hx else (abs(cx[0] - cy[0]), abs(cx[1] - cy[1]))
            if m_unit and across * m_unit <= 3.0 and along * m_unit <= 0.25 * max(lx, ly) and (best is None or across < best[0]):
                best = (across, y)
        if best:
            pairs[id(x)] = best[1]
    paired = {id(v) for v in pairs.values()} | set(pairs)
    for r_ in runs:
        if id(r_) in pairs:
            y = pairs[id(r_)]
            d_len = round(max(r_["size_m"]) - max(y["size_m"]), 2)
            moved_by = round(best_gap(r_, y, m_unit), 1)
            if abs(d_len) < 0.1:
                moved += [y, r_]
                continue
            lines.append({"id": f"wall:{y['id']}-{r_['id']}", "group": "walls",
                          "item": "Wall moved" + (f" near ‘{r_['near']}’" if r_.get("near") else ""),
                          "detail": f"#{y['id']} → #{r_['id']}: moved ≈ {moved_by} m, {max(y['size_m']):.1f} → {max(r_['size_m']):.1f} m long",
                          "delta": d_len, "unit": "m", "price": "partition", "per_height": True,
                          "how": "the same wall at a new place: only its change in length is priced",
                          "marks": [{"box": y["box_a"], "kind": "removed"}, {"box": r_["box_a"], "kind": "added"}]})
            continue
        if id(r_) in paired:
            continue
        add, long_ = r_["kind"] == "added", max(r_["size_m"])
        if approx:  # a scan's mark is padded on every side alike: take the padding seen across the run off its length
            long_ = max(WALL_MIN_M, long_ - max(0.0, min(r_["size_m"]) - 0.15))  # DfE L00 scan 7.43 -> 6.99 (true 6.49)
            run = r_.get("run_m")  # the run's own ink (_ink_run_m): 6.34-6.37 m on that scan / photo turned 0/90/180/270
            if run and 0.6 * long_ <= run <= long_:  # (box 7.0-8.7 m); a box that also holds a changed label (AFCEC
                long_ = max(WALL_MIN_M, run)        # A-2: run 1.8 of 15.6 m) keeps the box's length
        lines.append({"id": f"wall:{r_['id']}", "group": "walls",
                      "item": f"Wall {'added' if add else 'removed'}" + (f" near ‘{r_['near']}’" if r_.get("near") else ""),
                      "detail": f"#{r_['id']}: a {'≈ ' if approx else ''}{long_:.1f} m run {'only on the new' if add else 'only on the old'} "
                                f"version" + (" (measured on a scan / photo: approximate, check)" if approx else ""),
                      "delta": round(long_ if add else -long_, 2), "unit": "m", "price": "partition", "per_height": True,
                      "approx": approx, "how": "linework that appears / vanishes, one thin run = one wall; × the wall "
                      "height" + ("; measured on a scan: approximate" if approx else ""),
                      "marks": [{"box": r_["box_a"], "kind": r_["kind"]}]})
    still, no_walls = [], []
    for r_ in unmeasured:
        wm = r_.get("walls_m")
        if not wm:
            still.append(r_)
            continue
        d = round(wm["new"] - wm["old"], 2)
        if abs(d) < 0.5:  # no wall length changed: the same walls redrawn (moved), or no walls at all (a fitting)
            (moved if r_["kind"] == "changed" and max(wm["old"], wm["new"]) >= 0.5 else no_walls).append(r_)
            continue
        lines.append({"id": f"walls:{r_['id']}", "group": "walls",
                      "item": "Walls redrawn" + (f" near ‘{r_['near']}’" if r_.get("near") else ""),
                      "detail": f"#{r_['id']}: walls in this area ≈ {wm['old']:.1f} → {wm['new']:.1f} m (solid wall lines "
                                "measured on both sheets: approximate, ±15 % of what changed)",
                      "delta": d, "unit": "m", "price": "partition", "per_height": True, "approx": True,
                      "how": "length of the solid wall lines in the changed area on each sheet",
                      "marks": [{"box": r_["box_a"], "kind": r_["kind"]}]})
    unmeasured = still
    if no_walls:
        lines.append({"id": "walls:none", "group": "walls", "item": "Other drawing changes", "delta": 0, "unit": "m",
                      "price": None, "detail": f"{_n(len(no_walls), 'place')} (#{', #'.join(str(r_['id']) for r_ in no_walls)}): "
                      "no change in wall length (fittings, furniture, hatching, notes, small bits): not priced here",
                      "how": "changed linework without solid wall lines",
                      "marks": [{"box": r_["box_a"], "kind": r_["kind"]} for r_ in no_walls]})
    if moved:
        lines.append({"id": "walls:moved", "group": "walls", "item": "Linework moved", "delta": 0, "unit": "m", "price": None,
                      "detail": f"{_n(len(moved), 'place')} (#{', #'.join(str(r_['id']) for r_ in moved)}): the same lines at a "
                                "new place, no change in length (price as rework if already built)",
                      "how": "the same ink shifted by 0.1-3 m", "marks": [{"box": r_["box_a"], "kind": "changed"} for r_ in moved]})
    if edges:
        review.append({"id": "review:edges", "item": f"Edge of a hatched area changed in {_n(len(edges), 'place')}",
                       "why": "a thick line along hatching appeared or vanished (#" + ", #".join(str(r_["id"]) for r_ in edges)
                              + "): a roof, slab or paving edge rather than a partition, check by hand",
                       "marks": [{"box": r_["box_a"], "kind": r_["kind"]} for r_ in edges]})
    if unmeasured:
        review.append({"id": "review:walls", "item": f"Walls redrawn in {_n(len(unmeasured), 'place')}",
                       "why": "several lines changed together (#" + ", #".join(str(r_["id"]) for r_ in unmeasured)
                              + "): wall lengths there are not measured, check by hand",
                       "marks": [{"box": r_["box_a"], "kind": r_["kind"]} for r_ in unmeasured]})
    if walls_on_scan:
        review.append({"id": "review:scanwalls", "item": f"Linework changed in {_n(len(walls_on_scan), 'place')}",
                       "why": "on a scan / photo without a printed scale we do not price wall lengths: check by hand",
                       "marks": [{"box": r_["box_a"], "kind": r_["kind"]} for r_ in walls_on_scan]})
    sk_status = "n/a"
    if skirting is not None:
        sk_status = "done"
        for g in skirting.get("by_name") or []:
            lines.append({"id": f"skirting:{g['name']}", "group": "skirting", "item": g["name"], "detail": g["detail"],
                          "old": g["old"], "new": g["new"], "delta": round(g["new"] - g["old"], 2), "unit": "m",
                          "delta_net": round(g["new"] - g["old"], 2), "wet": bool(g.get("wet")),
                          "delta_whole": round(g.get("new_p", g["new"]) - g.get("old_p", g["old"]), 2),
                          "price": "skirting", "approx": True, "how": skirting.get("how", ""), "marks": g["marks"],
                          "outlines": g.get("outlines", [])})
        review += skirting.get("review") or []
    elif any(r_["name"] and not _no_skirting(r_["name"], True) for r_ in rooms):
        if res["a"]["kind"] == res["b"]["kind"] == "vector":
            sk_status = "pending"  # POST /cost traces the changed rooms
        else:
            review.append({"id": "review:skirting-scan", "item": "Skirting", "why": "room outlines are traced on vector "
                           "PDFs only: measure the skirting of the changed rooms by hand", "marks": []})
    prices = {p["key"]: p["eur"] for p in PRICES}
    for ln in lines:
        ln["eur"] = round(ln["delta"] * (WALL_H if ln.get("per_height") else 1.0) * prices[ln["price"]], 2) if ln["price"] else 0.0
    total = round(sum(ln["eur"] for ln in lines), 2)
    out = {"prices": PRICES, "wall_height_m": WALL_H, "lines": lines, "review": review, "total_eur": total,
           "skirting": sk_status, "currency": "EUR",
           "rule": "net quantity changes between the two versions; floor = printed room areas; doors = door sets "
                   "(a double door is 1 set); walls = solid wall lines that appear, vanish or are redrawn × the wall height; skirting = traced "
                   "room outline minus openings (stairs, lifts: none; tiled wet rooms: none unless the Conventions tab includes them); moved items are not priced; items to "
                   "check are not in the total",
           "prices_note": "example prices — use your own"}
    out["summary"] = cost_summary(out)
    return out


def cost_summary(c: dict) -> str:
    """One plain line at the given prices: 'This revision: floor area +4 m² (+€152) · … → +€2,142 (example prices)'."""
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for ln in c["lines"]:
        if ln["price"]:
            by[ln["group"]].append(ln)
    eur = lambda v: ("+" if v > 0 else "−" if v < 0 else "±") + f"€{abs(v):,.0f}"  # noqa: E731
    bits = []
    for g, word, u in (("floor", "floor area", "m²"), ("doors", "doors", ""), ("skirting", "skirting", "m"), ("walls", "walls", "m")):
        ls = by.get(g)
        if not ls:
            continue
        e = sum(ln["eur"] for ln in ls)
        if g == "doors":
            plus, minus = sum(ln["delta"] for ln in ls if ln["delta"] > 0), -sum(ln["delta"] for ln in ls if ln["delta"] < 0)
            q = " ".join(x for x in (f"+{plus}" if plus else "", f"−{minus}" if minus else "") if x) + (" door set" if plus + minus == 1 else " door sets")
        else:
            q = f"{word} {sum(ln['delta'] for ln in ls):+.1f} {u}".replace("+-", "−").replace("-", "−")
        bits.append(f"{q} ({eur(e)})")
    if not bits:
        return "No priced quantity changed." + (f" {_n(len(c['review']), 'item')} to check by hand." if c["review"] else "")
    s = "This revision: " + " · ".join(bits) + f" → {eur(c['total_eur'])} at {c['prices_note'].split(' —')[0]}"
    if c["review"]:
        s += f"; {_n(len(c['review']), 'item')} to check by hand (not in the total)"
    return s + "."


def _to_a(res: dict, pts: list) -> list:
    """Points in B's native units -> A's native units through the stored alignment (render px matrix + zooms)."""
    M = np.array(res["alignment"]["matrix"], float)
    zb, za = res["b"].get("zoom") or 1.0, res["a"].get("zoom") or 1.0
    p = np.c_[np.asarray(pts, float) * zb, np.ones(len(pts))] @ M.T
    return [[round(float(x / w / za), 2), round(float(y / w / za), 2)] for x, y, w in p]


def skirting_changes(res: dict, abs_a: str, abs_b: str, wet: bool = False) -> dict:
    """Skirting of every room whose printed area changed (or that is new / gone), on BOTH vector sheets: E1's room
    tracer at the room's own tag (stoa.evidence.lineplan._measure) and its skirting rule (outline minus openings,
    jamb to jamb: lineplan._skirting). A trace is used only when its area agrees with the room's printed area within
    TRACE_TOL; a room name with any copy that fails goes to 'review' (not priced). Stairs / lifts: none; wet rooms none
    unless `wet`. Each room carries both the net run (openings deducted) and the whole outline (old_p / new_p), so the
    panel follows F4's 'door openings deducted or not' without another trace."""
    out = {"by_name": [], "review": [], "rooms": 0, "traces": 0, "seconds": 0.0,
           "how": f"traced room outline minus door openings, on both sheets; each outline checked against the room's "
                  f"printed area (±{TRACE_TOL:.0%} of the printed figure, read as 12.5-13.5 when it says 13)"}
    if not (res["a"]["kind"] == res["b"]["kind"] == "vector" and res["a"]["space"] == "pdf_pt"):
        return out
    t0 = time.perf_counter()
    from stoa.evidence import lineplan
    from stoa.evidence.linework import sheet_scales

    pg_a, pg_b = res["a"]["page"] or 0, res["b"]["page"] or 0
    sa, sb = sheet_scales(abs_a, pg_a), sheet_scales(abs_b, pg_b)
    n_a = float(sa[0]) if sa else None
    n_b = float(sb[0]) if sb else n_a
    if not n_a:
        out["review"].append({"id": "review:skirting", "item": "Skirting", "why": "no printed scale on the sheet: "
                              "room outlines cannot be measured", "marks": []})
        return out
    out["wet"] = wet
    rooms = [r_ for r_ in (res.get("areas") or {}).get("rooms") or [] if not _no_skirting(r_["name"], wet)]
    if len(rooms) > SKIRT_MAX_ROOMS:
        out["review"].append({"id": "review:skirting-more", "item": f"Skirting in {len(rooms) - SKIRT_MAX_ROOMS} more rooms",
                              "why": f"only the first {SKIRT_MAX_ROOMS} changed rooms are traced", "marks": []})
        rooms = rooms[:SKIRT_MAX_ROOMS]

    def trace(path, page, n, box, printed, unit):
        pt = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        try:
            m = lineplan._measure(path, page, pt, n, tuple(box))
        except Exception:  # noqa: BLE001
            m = None
        out["traces"] += 1
        if m is None:
            return None, "its walls could not be traced around the label"
        k = FT2 if unit == "ft2" else 1.0
        want = printed * k
        # a printed '13 m2' is anything from 12.5 to 13.5: the tolerance starts from that range (DfE 2012 SEN Resource
        # and Food Bay '13 m2' trace 14.1 = +8.5 % of 13 but +4.4 % of 13.5; the 8 % alone sent them to 'check by hand')
        half = 0.5 if float(printed).is_integer() else 0.05
        if not (printed - half) * k * (1 - TRACE_TOL) <= m["area_m2"] <= (printed + half) * k * (1 + TRACE_TOL):
            return None, f"the traced outline ({m['area_m2']:.1f} m²) does not match the printed {_fmt_q(want)} m²"
        try:
            sk = lineplan._skirting(path, page, n, m)
        except Exception:  # noqa: BLE001
            sk = None
        if sk is None:
            return None, "no skirting reading for the traced outline"
        return {"value": float(sk["value_m"]), "perimeter": float(sk["perimeter_m"]), "openings": len(sk.get("openings") or []),
                "polygon": [[round(float(x), 2), round(float(y), 2)] for x, y in m["polygon"]]}, None

    groups: dict[str, dict] = {}
    for r_ in rooms:
        g = groups.setdefault(r_["name"].lower(), {"name": r_["name"], "old": 0.0, "new": 0.0, "n_old": 0, "n_new": 0,
                                                    "old_p": 0.0, "new_p": 0.0, "fail": None, "marks": [],
                                                    "outlines": [], "each": []})
        out["rooms"] += 1
        for side, path, page, n, box, printed in (("a", abs_a, pg_a, n_a, r_.get("box_a"), r_["old"]),
                                                  ("b", abs_b, pg_b, n_b, r_.get("box_b"), r_["new"])):
            if printed is None or not box or g["fail"]:
                continue
            got, why = trace(path, page, n, box, printed, r_["unit"])
            if got is None:
                g["fail"] = why
                continue
            poly = got["polygon"] if side == "a" else _to_a(res, got["polygon"])
            g["old" if side == "a" else "new"] += got["value"]
            g["old_p" if side == "a" else "new_p"] += got["perimeter"]
            g["n_old" if side == "a" else "n_new"] += 1
            g["outlines"].append({"polygon": poly, "kind": "removed" if side == "a" else "added"})
            g["each"].append((side, got["value"]))
        g["marks"] += _room_marks([r_], r_["name"])
    for g in groups.values():
        if g["fail"]:
            out["review"].append({"id": f"review:skirting:{g['name']}", "item": f"Skirting of {g['name']}",
                                  "why": g["fail"] + ": measure by hand", "marks": g["marks"]})
            continue
        if abs(g["new"] - g["old"]) < 0.05 and abs(g["new_p"] - g["old_p"]) < 0.05:
            continue
        olds = [v for s_, v in g["each"] if s_ == "a"]
        news = [v for s_, v in g["each"] if s_ == "b"]
        detail = (f"{' + '.join(f'{v:.1f}' for v in olds) or '—'} → {' + '.join(f'{v:.1f}' for v in news) or '—'} m"
                  if len(olds) + len(news) <= 6 else f"{len(olds)} → {_n(len(news), 'room')}")
        out["by_name"].append({"name": g["name"], "old": round(g["old"], 2), "new": round(g["new"], 2), "detail": detail,
                               "old_p": round(g["old_p"], 2), "new_p": round(g["new_p"], 2), "wet": _wet(g["name"]),
                               "marks": g["marks"], "outlines": g["outlines"]})
    out["by_name"].sort(key=lambda d: -abs(d["new"] - d["old"]))
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def _area_q(name: str) -> str:
    """'Staff Room' -> 'What is the area of the Staff Room?'; a code-like label ('Yr 4', 'U106') without 'the'."""
    return f"What is the area of {name}?" if any(c.isdigit() for c in name) else f"What is the area of the {name}?"


def suggest_questions(res: dict, skip: set[str] | None = None, max_rooms: int = 4) -> list[dict]:
    """Questions whose answers the edits may change (Stoa's kinds: areas > lengths > counts): rooms whose printed
    area changed first, then rooms next to changed linework, then door / window counts when linework changed."""
    out: list[dict] = []
    seen: set[str] = set()
    same: set[tuple] = set()  # one room per identical change: 'Yr 3' stands for Yr 3-6 (all 62 -> 55 m2)
    cand = []
    for d in (res.get("areas") or {}).get("by_name", []):
        change = (tuple(sorted(set(d["old"]))), tuple(sorted(set(d["new"]))), d.get("unit"))
        if d["name"].lower() in seen or _ROOM_STOP.search(d["name"]) or change in same:
            continue
        seen.add(d["name"].lower())
        same.add(change)
        cand.append(d)
    # by_name lists rooms on both versions first: keep 2 of them and 2 new / gone rooms (the DfE pair asked two locker
    # bays, -2 and -1 m2, and never the new 69 m2 Practical Science room), then fill up in the same order
    both = [d for d in cand if d.get("count_old", 1) and d.get("count_new", 1)]
    one = [d for d in cand if not (d.get("count_old", 1) and d.get("count_new", 1))]
    pick = both[:2] + one[:2]
    pick += [d for d in cand if d not in pick][:max(0, max_rooms - len(pick))]
    pick = sorted(pick[:max_rooms], key=cand.index)
    seen = {d["name"].lower() for d in pick}
    for d in pick:
        out.append({"question": _area_q(d["name"]), "kind": "area", "region": None})
    for rg in res.get("regions", []):
        name = rg.get("near")
        if len(seen) >= max_rooms:
            break
        if not name or _ROOM_STOP.search(name) or name.lower() in seen or _quantity(name):
            continue
        seen.add(name.lower())
        out.append({"question": _area_q(name), "kind": "area", "region": rg["id"]})
    if res.get("regions"):
        out.append({"question": "How many doors are there?", "kind": "count", "region": None})
        out.append({"question": "How many windows are there?", "kind": "count", "region": None})
    return out


def impact(res: dict, abs_a: str, abs_b: str, questions: list[str] | None = None, ask=None) -> list[dict]:
    """Ask each question on both versions through the pipeline; report old / new / delta with both answers'
    evidence and confidence. `ask(abs_path, virtual, question) -> Answer dict` (default: the upload path's
    in-process ask, web/server/ask_upload.answer, with Gemini / the verifier off: the cheap tier)."""
    qs = questions or [q["question"] for q in res.get("suggested_questions", [])]
    ask = ask or _ask_local
    rows = []
    for q in qs:
        oa = ask(abs_a, res["a"]["file"], q)
        ob = ask(abs_b, res["b"]["file"], q)
        va, vb = oa.get("value"), ob.get("value")
        row = {"question": q, "old": va, "new": vb, "unit": oa.get("unit") or ob.get("unit"),
               "confidence_old": oa.get("confidence"), "confidence_new": ob.get("confidence"),
               "evidence_old": oa.get("evidence") or [], "evidence_new": ob.get("evidence") or [],
               "solver_old": oa.get("solver"), "solver_new": ob.get("solver"), "delta": None, "changed": None}
        try:
            fa, fb = float(va), float(vb)
            row["delta"] = round(fb - fa, 4)
            row["changed"] = abs(fb - fa) > (0.5 if (row["unit"] in (None, "", "count")) else 0.01 * max(abs(fa), 1e-9))
        except (TypeError, ValueError):
            row["changed"] = None if va is None or vb is None else str(va).strip() != str(vb).strip()
        rows.append(row)
    return rows


def _ask_local(abs_path: str, virtual: str, question: str) -> dict:
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("_f5_ask_upload", root / "web" / "server" / "ask_upload.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.answer({"abs": abs_path, "virtual": virtual, "question": question,
                       "solver_args": {"gemini": 0, "verify": 0}, "private": True})  # T2 21:59: every hosted step off


# ------------------------------------------------------------------------------------------------------ router
def enabled() -> bool:
    """The registry's rule (QA 21:19: 'all' must count too): STOA_FEATURES lists this feature or says 'all'."""
    try:
        from stoa.features import enabled as on

        return FEATURE in on()
    except ImportError:  # pragma: no cover  (the registry is missing: read the flag directly)
        names = {s.strip().lower() for s in os.environ.get("STOA_FEATURES", "").split(",")}
        return FEATURE in names or "all" in names


def resolve(path: str) -> Path:
    """'upload/<id>/<name>' (the web upload folder) or a dataset path 'data/...'; nothing else."""
    if not path or ".." in Path(path).parts or path.startswith("/"):
        raise ValueError("path must be 'upload/<id>/<name>' or dataset-relative 'data/...'")
    if path.startswith("upload/"):
        p = (UPLOADS / path[len("upload/"):]).resolve()
        m = re.match(r"^upload/([^/\\]+)/([a-z0-9_]+)/([^/\\]+)$", path)
        if m and not p.is_file():  # F8 22:42: a picture whose drawing style the upload guessed ('cubicasa5k') is shown
            # as upload/<id>/<style>/<name> but stored at <uploads>/<id>/data/<style>/<name> (web/mock/stoaApi.ts)
            p = (UPLOADS / m.group(1) / "data" / m.group(2) / m.group(3)).resolve()
        if UPLOADS.resolve() not in p.parents:
            raise ValueError("path outside the upload folder")
    else:
        from stoa.data import abspath, data_root

        p = abspath(path).resolve()
        if data_root().resolve() not in p.parents:
            raise ValueError("path outside the dataset")
    if not p.is_file():
        raise FileNotFoundError(path)
    return p


# One-click examples (captain 21:13: the real DfE revision as a catalogue card). Files are served to the browser, which
# uploads them like a visitor would (same upload folder, warm-up and questions as any upload).
DEMO_DIR = Path(os.environ.get("STOA_F5_DEMO_DIR", Path(os.environ.get(
    "JUNCTION_SHARED", ".stoa")) / "datasets" / "stoa-like" / "dfe-baseline-2014"))
DEMOS = {
    "dfe630": {
        "title": "Primary school (630 places), first floor: the classrooms re-planned from 62 to 55 m²",
        "a": "baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_ff_62m2.pdf",
        "b": "baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_ff_with_55m2_classrooms.pdf",
        "a_name": "school-first-floor-62m2-classrooms.pdf", "b_name": "school-first-floor-55m2-classrooms.pdf",
        # what the panel calls the two versions (UX NEW-11: not the file names)
        "a_label": {"title": "Primary school, first floor", "version": "issue A (62 m² classrooms)"},
        "b_label": {"title": "Primary school, first floor", "version": "issue B (55 m² classrooms)"},
        "credit": "Drawings: Education Funding Agency (DfE) baseline designs, 2014, Open Government Licence v3.0",
    },
    # a whole issue: two 2-page sets (ground + first floor), pages in another order in the new issue; built on demand
    "dfe630set": {
        "title": "Primary school (630 places), two issues of the ground and first floor: which sheet changed?",
        "a": ["baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_gf_002.pdf",
              "baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_ff_62m2.pdf"],
        "b": ["baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_ff_with_55m2_classrooms.pdf",
              "baseline-designs-630-place-primary-schoo__3fe_630_place_jmi_primary_school_gf_002.pdf"],
        "a_name": "school-issue-1.pdf", "b_name": "school-issue-2.pdf",
        "a_label": {"version": "issue 1"}, "b_label": {"version": "issue 2"},
        "credit": "Drawings: Education Funding Agency (DfE) baseline designs, 2014, Open Government Licence v3.0",
        "thumb_pages": (1, 0),  # the catalogue picture: the first floor (A's 2nd sheet = B's 1st), the sheet that changed
    },
}


def _demo_names(res: dict) -> None:
    """The one-click examples' versions get the names the panel shows ('issue A (62 m² classrooms)'), by file name."""
    for side in ("a", "b"):
        nm = (res.get(side) or {}).get("name")
        if not nm:
            continue
        for d in DEMOS.values():
            for which in ("a", "b"):
                if nm.get("file") == d.get(f"{which}_name") and d.get(f"{which}_label"):
                    nm.update(d[f"{which}_label"])


def _demo_files(d: dict, which: str) -> list[str]:
    v = d[which]
    return [v] if isinstance(v, str) else list(v)


def demo_available(demo: str) -> bool:
    d = DEMOS[demo]
    return all((DEMO_DIR / f).is_file() for w in ("a", "b") for f in _demo_files(d, w))


def demo_path(demo: str, which: str) -> Path:
    d = DEMOS.get(demo)
    if not d or which not in ("a", "b"):
        raise KeyError(f"{demo}/{which}")
    srcs = [DEMO_DIR / f for f in _demo_files(d, which)]
    for p in srcs:
        if not p.is_file():
            raise FileNotFoundError(f"{p.name} is not on this server (STOA_F5_DEMO_DIR)")
    if len(srcs) == 1:
        return srcs[0]
    out = CACHE_DIR / "demo" / f"{demo}-{which}.pdf"  # a set: its sheets as one PDF, one page each
    if not out.is_file() or out.stat().st_mtime < max(p.stat().st_mtime for p in srcs):
        import fitz

        out.parent.mkdir(parents=True, exist_ok=True)
        doc = fitz.open()
        for p in srcs:
            with fitz.open(str(p)) as x:
                doc.insert_pdf(x)
        tmp = out.with_suffix(".tmp.pdf")
        doc.save(str(tmp))
        doc.close()
        os.replace(tmp, out)
    return out


def change_window(overlay: np.ndarray, frac: float = 0.42, aspect: float = 16 / 9) -> tuple[int, int, int, int]:
    """The aspect-shaped window (``frac`` of the image width) holding the most change ink of an overlay (red removed,
    green added, orange text boxes): (x0, y0, x1, y1) in pixels. The whole image when nothing is coloured."""
    h, w = overlay.shape[:2]
    b, g, r = (overlay[..., i].astype(np.int16) for i in range(3))
    ink = ((np.abs(r - g) > 60) | (np.abs(g - r) > 60) | (np.abs(r - b) > 90)).astype(np.float64)
    ww = max(16, min(w, int(round(frac * w))))
    wh = max(9, min(h, int(round(ww / aspect))))
    if not ink.any():
        return 0, 0, w, h
    ii = np.pad(ink.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    step = max(1, min(w, h) // 120)
    ys = np.arange(0, h - wh + 1, step)
    xs = np.arange(0, w - ww + 1, step)
    Y, X = np.meshgrid(ys, xs, indexing="ij")
    tot = ii[Y + wh, X + ww] - ii[Y, X + ww] - ii[Y + wh, X] + ii[Y, X]
    k = np.unravel_index(int(np.argmax(tot)), tot.shape)
    x0, y0 = int(X[k]), int(Y[k])
    return x0, y0, x0 + ww, y0 + wh


def demo_thumb(demo: str, width: int = 960) -> Path:
    """The catalogue card's picture of an example (UX 23:0x: the card showed a generic icon): its compare overlay
    (removed red, added green) cut to the 16:9 window with the most changes, strokes thickened so they survive the
    downscale. Made once (one compare, ~3 s) and kept next to the demo files' cache."""
    import cv2

    d = DEMOS.get(demo)
    if not d:
        raise KeyError(demo)
    srcs = [DEMO_DIR / f for w in ("a", "b") for f in _demo_files(d, w)]
    out = CACHE_DIR / "demo" / f"{demo}-thumb-{width}.png"
    if out.is_file() and all(p.is_file() for p in srcs) and out.stat().st_mtime >= max(p.stat().st_mtime for p in srcs):
        return out
    pa, pb = demo_path(demo, "a"), demo_path(demo, "b")
    page_a, page_b = d.get("thumb_pages", (0, 0))
    work = CACHE_DIR / "demo" / f"{demo}-thumb"
    res = compare(str(pa), str(pb), page_a, page_b, out_dir=work)
    img = cv2.imread(str(work / "overlay.png"), cv2.IMREAD_COLOR)
    if img is None or not res["alignment"]["ok"]:
        raise FileNotFoundError(f"{demo}: no overlay")
    x0, y0, x1, y1 = change_window(img)
    crop = img[y0:y1, x0:x1]
    f = max(1, int(round((x1 - x0) / width)))
    if f > 1:  # a min filter thickens dark and coloured strokes before INTER_AREA averages them away
        crop = cv2.erode(crop, np.ones((f, f), np.uint8))
    hh = int(round(crop.shape[0] * width / crop.shape[1]))
    crop = cv2.resize(crop, (width, hh), interpolation=cv2.INTER_AREA)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.png")
    cv2.imwrite(str(tmp), crop, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    os.replace(tmp, out)
    return out


_RESULTS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_LOCK = threading.Lock()


def _layers_ready(*paths: Path) -> bool:
    """Every scan among the files has its OCR text layer now (the browser asks again while 'areas_pending')."""
    try:
        from stoa.solvers.raster.sheet import cached_layer
    except Exception:  # noqa: BLE001
        return False
    return all(cached_layer(str(p)) is not None for p in paths if p.suffix.lower() in IMAGE_EXT)


def _turned_b(pa: Path, page_a: int, pb: Path, page_b: int | None) -> tuple[Path, int | None, str | None]:
    """B re-plotted TURNED (its content drawn at 90 / 180 / 270 deg, no /Rotate flag) while A reads upright: compare
    against B's upright copy, an exact vector pair again. The DfE first floor turned 90 / 180 deg was priced +EUR 326 /
    -EUR 2,028 against B as it is (wall lines rasterised turned and warped back differ by a pixel), +EUR 380 against
    the copy = the upright pair. Returns (path, page, note); B itself (note None) when nothing is turned."""
    if not UPRIGHT or pb.suffix.lower() != ".pdf" or pa.suffix.lower() != ".pdf":
        return pb, page_b, None
    try:
        import fitz

        with fitz.open(str(pb)) as d:
            n = len(d)
        pg = page_b if page_b is not None and page_b >= 0 else (0 if n == 1 else None)
        if pg is None or pg >= n or _upright_copy(str(pa), page_a) is not None:
            return pb, page_b, None
        u = _upright_copy(str(pb), pg)
    except Exception:  # noqa: BLE001 - compare B as it is
        return pb, page_b, None
    if u is None:
        return pb, page_b, None
    deg = round(math.degrees(math.atan2(float(u[1][1][0]), float(u[1][0][0])))) % 360
    how = "upside down" if deg == 180 else "turned a quarter turn"  # 90 or 270: which way depends on how you look
    return Path(u[0]), 0, f"The other version is drawn {how} on its page: it was compared on a copy turned back upright"


# ---- two versions of a MODEL (IFC): elements matched by GlobalId, exact counts / areas / lengths --------------------
_IFC_KIND = {"IfcDoor": "door", "IfcWindow": "window", "IfcWall": "wall", "IfcWallStandardCase": "wall",
             "IfcCurtainWall": "curtain wall", "IfcSpace": "room", "IfcSlab": "slab", "IfcRoof": "roof",
             "IfcStair": "stair", "IfcStairFlight": "stair flight", "IfcRamp": "ramp", "IfcColumn": "column",
             "IfcBeam": "beam", "IfcRailing": "railing", "IfcCovering": "covering", "IfcPlate": "plate",
             "IfcMember": "member", "IfcFooting": "footing", "IfcFurnishingElement": "furniture",
             "IfcFurniture": "furniture", "IfcSanitaryTerminal": "sanitary fitting", "IfcFlowTerminal": "fitting"}
IFC_MOVE_M = float(os.environ.get("STOA_F5_IFC_MOVE", "0.05"))  # an element whose box centre moved more = moved
IFC_OTHER = 0.3  # fewer elements than this share (by id or by place) = two different models


def _plural(kind: str) -> str:
    return kind if kind == "furniture" else kind + "s"


def _ifc_kind(cls: str) -> str:
    return _IFC_KIND.get(cls) or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cls.removeprefix("Ifc")).lower()


def _ifc_centre(e) -> tuple[float, float, float] | None:
    bb = getattr(e.geo, "bbox", None) if e.geo else None
    return ((bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2, (bb[2] + bb[5]) / 2) if bb else None


def _ifc_area(e) -> float | None:
    """A room's floor area: its NetFloorArea / GrossFloorArea quantity, else the footprint of its geometry."""
    q = e.qto or {}
    for suf in ("NetFloorArea", "GrossFloorArea"):
        v = next((v for k, v in q.items() if k.endswith(suf) and isinstance(v, (int, float)) and v > 0), None)
        if v:
            return float(v)
    fa = getattr(e.geo, "footprint_area", None) if e.geo else None
    return float(fa) if fa else None


def _ifc_len(e) -> float | None:
    """A wall's length: its axis, else its Length quantity, else the long side of its box."""
    if e.axis_length:
        return float(e.axis_length)
    q = e.qto or {}
    v = next((v for k, v in q.items() if k.endswith(".Length") and isinstance(v, (int, float)) and v > 0), None)
    if v:
        return float(v)
    bb = getattr(e.geo, "bbox", None) if e.geo else None
    return float(max(bb[3] - bb[0], bb[4] - bb[1])) if bb else None


def _ifc_height(e) -> float | None:
    q = e.qto or {}
    v = next((v for k, v in q.items() if k.endswith(".Height") and isinstance(v, (int, float)) and v > 0), None)
    if v:
        return float(v)
    bb = getattr(e.geo, "bbox", None) if e.geo else None
    return float(bb[5] - bb[2]) if bb and bb[5] > bb[2] else None


def _ifc_room_name(e) -> str:
    n, ln = (e.name or "").strip(), (e.long_name or "").strip()
    return ln or n or "room" if not (ln and n and ln != n) else ln


def _ifc_external(e) -> bool:
    v = next((v for k, v in (e.props or {}).items() if k.endswith(".IsExternal")), None)
    return bool(v) if v is not None else False


def ifc_compare(pa: Path, pb: Path, virtual_a: str | None = None, virtual_b: str | None = None) -> dict:
    """Two versions of one model: every element (doors, windows, walls, rooms, slabs …) matched by its GlobalId (an
    element exported with a new id: same class at the same place); per element added / removed / moved (box centre
    > IFC_MOVE_M) / resized (room area, wall length, door width or height) / renamed. Exact: no drawing is read."""
    from stoa.solvers.ifc.index import load_index

    t0 = time.time()
    ia, ib = load_index(str(pa)), load_index(str(pb))
    A = {g: e for g, e in ia.elements.items() if e.cls in _IFC_KIND or e.geo}
    B = {g: e for g, e in ib.elements.items() if e.cls in _IFC_KIND or e.geo}
    pairs = {g: g for g in A if g in B and A[g].cls == B[g].cls}
    by_id = len(pairs)
    # exported anew (new ids): the same class at the same place (box centre within 5 cm) is the same element
    la = [g for g in A if g not in pairs]
    free_b = {g for g in B if g not in set(pairs.values())}
    # a grid of IFC_MOVE_M cells per class: each element looks only at the 27 cells around its own (linear, not n²)
    cell = max(IFC_MOVE_M, 1e-3)
    grid: dict[tuple, list[str]] = collections.defaultdict(list)
    for h in free_b:
        cb = _ifc_centre(B[h])
        if cb is not None:
            grid[(B[h].cls, *(math.floor(v / cell) for v in cb))].append(h)
    for g in la:
        ca = _ifc_centre(A[g])
        if ca is None:
            continue
        k0 = [math.floor(v / cell) for v in ca]
        near = [h for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                for h in grid.get((A[g].cls, k0[0] + dx, k0[1] + dy, k0[2] + dz), ()) if h in free_b]
        best = min(near, key=lambda h: math.dist(ca, _ifc_centre(B[h])), default=None)
        if best is not None and math.dist(ca, _ifc_centre(B[best])) <= IFC_MOVE_M:
            pairs[g] = best
            free_b.discard(best)
    strong = len(pairs)  # by id or by place: evidence that the two files are one model (a shared family name is not)
    # no geometry in the index (furniture, an aggregated roof): the same class, name, type and storey, in file order
    pool: dict[tuple, list[str]] = collections.defaultdict(list)
    sname_b = {s_.guid: s_.name for s_ in ib.storeys}
    sname_a = {s_.guid: s_.name for s_ in ia.storeys}
    for h in B:
        if h in free_b and _ifc_centre(B[h]) is None:
            pool[(B[h].cls, B[h].name, B[h].type_name, sname_b.get(B[h].storey))].append(h)
    for g in la:
        if g in pairs or _ifc_centre(A[g]) is not None:
            continue
        hs = pool.get((A[g].cls, A[g].name, A[g].type_name, sname_a.get(A[g].storey)))
        if hs:
            pairs[g] = hs.pop(0)
            free_b.discard(pairs[g])
    st_a = {s.guid: s.name for s in ia.storeys}
    st_b = {s.guid: s.name for s in ib.storeys}
    elements: list[dict] = []

    def el(kind, e, eb=None, **kw):
        return {"change": kind, "cls": e.cls, "kind": _ifc_kind(e.cls),
                "name": (_ifc_room_name(e) if e.cls == "IfcSpace" else (e.name or e.type_name or "")).strip()[:80],
                "storey": (st_a.get(e.storey) if eb is None or kind != "added" else st_b.get(e.storey)) or "",
                "guid_a": e.guid if kind != "added" else None, "guid_b": (eb or e).guid if kind != "removed" else None, **kw}

    for g, h in pairs.items():
        ea, eb = A[g], B[h]
        ca, cb = _ifc_centre(ea), _ifc_centre(eb)
        moved = round(math.dist(ca, cb), 2) if ca and cb and math.dist(ca, cb) > IFC_MOVE_M else None
        what = []
        if ea.cls == "IfcSpace":
            xa, xb = _ifc_area(ea), _ifc_area(eb)
            if xa and xb and abs(xb - xa) > max(0.05, 0.01 * xa):
                what.append(("area", round(xa, 2), round(xb, 2), "m²"))
            if _ifc_room_name(ea) != _ifc_room_name(eb):
                what.append(("name", _ifc_room_name(ea), _ifc_room_name(eb), ""))
        elif ea.cls in ("IfcWall", "IfcWallStandardCase", "IfcCurtainWall"):
            xa, xb = _ifc_len(ea), _ifc_len(eb)
            if xa and xb and abs(xb - xa) > 0.05:
                what.append(("length", round(xa, 2), round(xb, 2), "m"))
        elif ea.cls in ("IfcDoor", "IfcWindow"):
            for k in ("OverallWidth", "OverallHeight"):
                xa, xb = (ea.dims or {}).get(k), (eb.dims or {}).get(k)
                if xa and xb and abs(xb - xa) > 0.01:
                    what.append(("width" if k.endswith("Width") else "height", round(xa, 3), round(xb, 3), "m"))
            if (ea.type_name or "") != (eb.type_name or "") and ea.type_name and eb.type_name:
                what.append(("type", ea.type_name, eb.type_name, ""))
        if moved or what:
            elements.append(el("changed" if what else "moved", ea, eb, moved_m=moved,
                               what=[{"what": w, "old": o, "new": n, "unit": u} for w, o, n, u in what]))
    matched_b = set(pairs.values())
    for g, e in A.items():
        if g not in pairs:
            elements.append(el("removed", e))
    for h, e in B.items():
        if h not in matched_b:
            elements.append(el("added", e, e))
    share = len(pairs) / max(1, min(len(A), len(B)))
    other = strong / max(1, min(len(A), len(B))) < IFC_OTHER
    # per class: counts before / after and what happened
    by_class: dict[str, dict] = {}
    for e in A.values():
        by_class.setdefault(_ifc_kind(e.cls), {"kind": _ifc_kind(e.cls), "old": 0, "new": 0})["old"] += 1
    for e in B.values():
        by_class.setdefault(_ifc_kind(e.cls), {"kind": _ifc_kind(e.cls), "old": 0, "new": 0})["new"] += 1
    for x in elements:
        d = by_class[x["kind"]]
        d[x["change"]] = d.get(x["change"], 0) + 1
    classes = sorted((d for d in by_class.values() if any(d.get(k) for k in ("added", "removed", "moved", "changed"))),
                     key=lambda d: (d["kind"] not in ("door", "window", "wall", "room"), -(d.get("added", 0) + d.get("removed", 0) + d.get("changed", 0) + d.get("moved", 0))))
    # rooms in the by-name shape of the printed areas, so the panel's room list and the cost's floor lines read them
    rooms_old: dict[str, list[float]] = collections.defaultdict(list)
    rooms_new: dict[str, list[float]] = collections.defaultdict(list)
    for e in A.values():
        if e.cls == "IfcSpace" and _ifc_area(e):
            rooms_old[_ifc_room_name(e)].append(round(_ifc_area(e), 2))
    # a room is its id: a renamed room keeps one line, named 'old → new' (not 'gone' + 'new')
    partner = {h: g for g, h in pairs.items()}
    renamed: dict[str, str] = {}
    for h, e in B.items():
        if e.cls == "IfcSpace" and _ifc_area(e):
            g = partner.get(h)
            na, nb = (_ifc_room_name(A[g]) if g else None), _ifc_room_name(e)
            if na and na != nb:
                renamed[na] = f"{na} → {nb}"
            rooms_new[na or nb].append(round(_ifc_area(e), 2))
    by_name = []
    for n in sorted(set(rooms_old) | set(rooms_new)):
        o, w = rooms_old.get(n, []), rooms_new.get(n, [])
        if sorted(o) == sorted(w):
            continue
        ch = [[x["what"][0]["old"], x["what"][0]["new"]] for x in elements if x["cls"] == "IfcSpace" and x["name"] == n
              and x.get("what") and x["what"][0]["what"] == "area"]
        by_name.append({"name": renamed.get(n, n), "unit": "m2", "old": o, "new": w, "count_old": len(o), "count_new": len(w),
                        "sum_old": round(sum(o), 3), "sum_new": round(sum(w), 3), "delta": round(sum(w) - sum(o), 3),
                        "changed": ch})
    by_name.sort(key=lambda d: (not (d["count_old"] == d["count_new"] > 0), -abs(d["delta"])))
    so, sn = sum(sum(v) for v in rooms_old.values()), sum(sum(v) for v in rooms_new.values())
    total = {"unit": "m2", "old": round(so, 3), "new": round(sn, 3), "delta": round(sn - so, 3),
             "rooms_old": sum(map(len, rooms_old.values())), "rooms_new": sum(map(len, rooms_new.values()))} if (so or sn) else None
    nd = by_class.get("door", {"old": 0, "new": 0})
    parts = [f"{_plural(d['kind'])} {d['old']} → {d['new']}" + "".join(f", {d[k]} {k}" for k in ("added", "removed", "moved", "changed") if d.get(k))
             for d in classes[:5]]
    how = (f"by the element ids (GlobalId) the two models share ({by_id} of {min(len(A), len(B))})"
           + (f"; {len(pairs) - by_id} more by class and place (exported with new ids)" if len(pairs) > by_id else ""))
    name_a, name_b = ia.project_name or Path(virtual_a or pa.name).stem, ib.project_name or Path(virtual_b or pb.name).stem
    res = {"model": True, "a": {"file": virtual_a or str(pa), "page": 0, "space": "ifc", "size": None,
                               "name": {"title": name_a, "number": None, "revision": None, "source": "model", "file": pa.name}},
           "b": {"file": virtual_b or str(pb), "page": 0, "space": "ifc", "size": None,
                 "name": {"title": name_b, "number": None, "revision": None, "source": "model", "file": pb.name}},
           "alignment": {"method": "ids", "ok": not other, "overlap": round(share, 3), "residual": 0.0, "scale": 1.0,
                         "rotation_deg": 0.0, "how": how, "plain": how},
           "regions": [], "text": [], "quantities": [], "areas": {"rooms": [], "by_name": by_name, "total": total},
           "doors": None, "scale": None, "page_b_note": None, "suggested_questions": [],
           "same_sheet": not other, "other_sheet": other, "approximate": False, "changed_share": round(1 - share, 3),
           "drawing_kind": {"a": "model", "b": "model"},
           "model_changes": {"by_class": classes, "elements": elements[:400], "n_elements": len(elements),
                             "count_a": len(A), "count_b": len(B), "matched": len(pairs), "by_id": by_id},
           "wall_seconds": round(time.time() - t0, 2)}
    res["summary"] = ("These look like two different models: only " f"{share:.0%} of the elements match; nothing is priced."
                      if other else ("What changed in the model: " + "; ".join(parts) + "." if parts else "No differences found in the model."))
    if total and total["delta"] and not other:
        res["summary"] += f" Room areas {_fmt_q(total['old'])} → {_fmt_q(total['new'])} m² ({total['delta']:+,.1f})."
    res["revision_cost"] = ifc_cost(res, A, B, pairs, elements)
    return res


def ifc_cost(res: dict, A: dict, B: dict, pairs: dict, elements: list[dict]) -> dict:
    """The revision's cost from the model's own quantities (example prices): floors from room areas, door sets from
    the doors added / removed, partitions from the inner walls added / removed (length × the wall's own height)."""
    _eur = {p["key"]: p["eur"] for p in PRICES}
    if res.get("other_sheet"):
        why = "these look like two different models"
        return {"prices": PRICES, "wall_height_m": WALL_H, "lines": [], "total_eur": 0.0, "skirting": "n/a",
                "review": [{"id": "review:withheld", "item": "Not priced", "why": why, "marks": []}], "currency": "EUR",
                "withheld": why, "rule": "only two versions of the same model are priced",
                "prices_note": "example prices — use your own", "summary": f"Not priced: {why}."}
    lines, review = [], []
    for d in res["areas"]["by_name"]:
        if not d["delta"]:
            continue
        detail = (_partial(d, "m²") or (f"new room: {_each(d['new'], 'm²')}" if not d["count_old"] else
                  f"room gone: {_each(d['old'], 'm²')}" if not d["count_new"] else f"{_each(d['old'], 'm²')} → {_each(d['new'], 'm²')}"))
        pk = "floor_wet" if _wet(d["name"]) else "floor"
        lines.append({"id": f"floor:{d['name']}", "group": "floor", "item": d["name"], "detail": detail,
                      "old": round(d["sum_old"], 2), "new": round(d["sum_new"], 2), "delta": round(d["delta"], 2), "unit": "m²",
                      "price": pk, "per_height": False, "approx": False, "how": "room areas in the two models", "marks": [],
                      "eur": round(d["delta"] * _eur[pk], 2)})
    add = [x for x in elements if x["cls"] == "IfcDoor" and x["change"] == "added"]
    rem = [x for x in elements if x["cls"] == "IfcDoor" and x["change"] == "removed"]
    for xs, sign, item, where in ((add, 1, "Doors added", "only in the new model"), (rem, -1, "Doors taken out", "only in the old model")):
        if xs:
            lines.append({"id": f"doors:{'added' if sign > 0 else 'removed'}", "group": "doors", "item": item,
                          "detail": f"{_n(len(xs), 'door')} {where}", "old": None, "new": None, "delta": sign * len(xs),
                          "unit": "set", "price": "door", "per_height": False, "approx": False,
                          "how": "doors in the two models (one IfcDoor = one door set)", "marks": [],
                          "guids": [x["guid_a"] or x["guid_b"] for x in xs], "eur": round(sign * len(xs) * _eur["door"], 2)})
    wall_cls = ("IfcWall", "IfcWallStandardCase")
    for x in elements:
        if x["cls"] not in wall_cls:
            continue
        e = A.get(x["guid_a"]) if x["guid_a"] else B.get(x["guid_b"])
        if x["change"] in ("added", "removed") or any(w["what"] == "length" for w in x.get("what") or []):
            if _ifc_external(e):
                review.append({"id": f"review:wall:{x['guid_a'] or x['guid_b']}", "item": f"Outside wall {x['change']}",
                               "why": "an outside wall is not a partition: price it by hand", "marks": [],
                               "guids": [x["guid_a"] or x["guid_b"]]})
                continue
            if x["change"] == "added":
                ln, sign = _ifc_len(e), 1
            elif x["change"] == "removed":
                ln, sign = _ifc_len(e), -1
            else:
                w = next(w for w in x["what"] if w["what"] == "length")
                ln, sign = w["new"] - w["old"], 1
            h = _ifc_height(e) or WALL_H
            if not ln:
                continue
            area = round(sign * ln * h, 2)
            lines.append({"id": f"walls:{x['guid_a'] or x['guid_b']}", "group": "walls",
                          "item": f"Wall {x['change']}" + (f" ({x['storey']})" if x["storey"] else ""),
                          "detail": f"{abs(ln):.2f} m × {h:.2f} m high" + (" longer" if x["change"] == "changed" and ln > 0 else " shorter" if x["change"] == "changed" else ""),
                          "old": None, "new": None, "delta": area, "unit": "m²", "price": "partition", "per_height": False,
                          "approx": False, "how": "the wall's own length and height in the model", "marks": [],
                          "guids": [x["guid_a"] or x["guid_b"]], "eur": round(area * _eur["partition"], 2)})
    total = round(sum(ln["eur"] for ln in lines), 2)
    groups = collections.OrderedDict()
    for ln in lines:
        groups[ln["group"]] = groups.get(ln["group"], 0.0) + ln["eur"]
    words = {"floor": "floor", "doors": "doors", "walls": "walls"}
    summ = " · ".join(f"{words[g]} {'+' if v >= 0 else '−'}€{abs(v):,.0f}" for g, v in groups.items())
    return {"prices": PRICES, "wall_height_m": WALL_H, "lines": lines, "review": review, "total_eur": total,
            "skirting": "n/a", "currency": "EUR",
            "rule": "net changes between the two models from their own quantities: floor = room areas; doors = door sets "
                    "(one IfcDoor = 1 set); walls = inner walls added, removed or resized × their own height",
            "prices_note": "example prices — use your own",
            "summary": f"This revision: {summ} → {'+' if total >= 0 else '−'}€{abs(total):,.0f} at example prices"
                       + (f"; {_n(len(review), 'item')} to check by hand (not in the total)." if review else ".")
                       if lines else "No priced quantity changed."}


def _sheet_of_cad(p: Path) -> Path:
    """A CAD file (.dxf) is compared as the sheet drawn from it: the same-stem PDF beside it (the dataset's Swiss
    floors; an upload's sheet made by web/server/normalize_upload.py)."""
    if p.suffix.lower() == ".dxf":
        for q in (p.with_suffix(".pdf"), p.with_suffix(".png")):
            if q.exists():
                return q
        raise ValueError(f"{p.name} is a CAD file without a sheet drawn from it: upload it (it is drawn as a sheet) "
                         "or print it to PDF")
    return p


def cached_compare(a: str, b: str, page_a: int = 0, page_b: int | None = None) -> dict:
    pa, pb = _sheet_of_cad(resolve(a)), _sheet_of_cad(resolve(b))
    st_a, st_b = pa.stat(), pb.stat()
    key = hashlib.sha256(json.dumps([str(pa), st_a.st_mtime_ns, st_a.st_size, page_a, str(pb), st_b.st_mtime_ns,
                                     st_b.st_size, page_b, 9]).encode()).hexdigest()[:16]
    with _LOCK:
        hit = _RESULTS.get(key)
    if hit is not None and not (hit.get("areas_pending") and _layers_ready(pa, pb)):
        with _LOCK:
            if key in _RESULTS:
                _RESULTS.move_to_end(key)
        out = {**hit, "cached": True}
        if hit.get("skirting") is not None:  # /cost traced the rooms already: the cost lines include skirting
            out["revision_cost"] = revision_cost(hit, hit["skirting"])
        return out
    ifc = (pa.suffix.lower() == ".ifc", pb.suffix.lower() == ".ifc")
    if any(ifc) and not all(ifc):
        raise ValueError("compare a model (IFC) with another version of the model, a sheet (PDF, image, DXF) with a sheet")
    if all(ifc):
        res = ifc_compare(pa, pb, virtual_a=a, virtual_b=b)
        res["key"] = key
        # no images, but a folder named by the key: privacy's 'Delete my files' finds a comparison by its folder (T2
        # recomputes this key for the visitor's files) and then drops the result held here too
        try:
            (CACHE_DIR / key).mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / key / "model.json").write_text('{"model": true}')
        except OSError:
            pass
        res["images"] = None
        res["cost"] = {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0, "api_calls": 0, "cpu_seconds": 0.0,
                       "ocr_crops": 0, "wall_seconds": res.get("wall_seconds", 0), "tier": "cheap"}
        with _LOCK:
            _RESULTS[key] = res
            while len(_RESULTS) > 64:
                _RESULTS.popitem(last=False)
        return {**res, "cached": False}
    pb_eff, page_b_eff, turned = _turned_b(pa, page_a, pb, page_b)
    res = compare(str(pa), str(pb_eff), page_a if pa.suffix.lower() == ".pdf" else 0,
                  page_b_eff if pb.suffix.lower() == ".pdf" else 0, virtual_a=a, virtual_b=b, out_dir=CACHE_DIR / key)
    if turned:  # B's own boxes (box_b) are in the upright copy's points; everything drawn on A is unaffected
        res["b"]["page"] = page_b if page_b is not None and page_b >= 0 else 0
        res["b"]["frame"] = "upright copy"
        res["page_b_note"] = (res.get("page_b_note") + " " if res.get("page_b_note") else "") + turned + "."
    res["key"] = key
    _demo_names(res)
    res["images"] = {n: f"/feat/{FEATURE}/image/{key}/{n}.png" for n in ("overlay", "b_aligned", "a")}
    oc = (res.get("text_check") or {}).get("ocr") or {}
    res["cost"] = {"llm_tokens_in": 0, "llm_tokens_out": 0, "gpu_seconds": 0, "api_calls": 0,
                   "cpu_seconds": round(float(oc.get("cpu_seconds") or 0.0), 2),  # one PP-OCR read of the changed labels
                   "ocr_crops": int(oc.get("crops") or 0), "wall_seconds": res.get("wall_seconds", 0), "tier": "cheap"}
    with _LOCK:
        _RESULTS[key] = res
        while len(_RESULTS) > 64:
            _RESULTS.popitem(last=False)
    return {**res, "cached": False}


def cached_cost(a: str, b: str, page_a: int = 0, page_b: int | None = None, wet: bool = False) -> dict:
    """The revision's cost lines WITH skirting (each changed room traced on both sheets, ~0.5 s per trace; cached with
    the compare). Same arguments as the compare call, so it hits the same cache entry."""
    res = cached_compare(a, b, page_a, page_b)
    if not res["alignment"]["ok"]:
        return {"key": res.get("key"), "revision_cost": None}
    if res.get("model"):  # a model's cost comes from its own quantities: nothing to trace
        return {"key": res.get("key"), "revision_cost": res.get("revision_cost"), "skirting": None}
    if withheld(res):
        return {"key": res.get("key"), "revision_cost": revision_cost(res), "skirting": None}
    slot = "skirting_wet" if wet else "skirting"  # F4's Conventions 'wet rooms included': traced separately
    sk = res.get(slot)
    if sk is None:
        pa, pb = _sheet_of_cad(resolve(a)), _sheet_of_cad(resolve(b))
        if res.get("b", {}).get("frame") == "upright copy":  # traced on the same upright copy (its only page)
            sk = skirting_changes({**res, "b": {**res["b"], "page": 0}}, str(pa), str(_turned_b(pa, page_a, pb, page_b)[0]),
                                  wet=wet)
        else:
            sk = skirting_changes(res, str(pa), str(pb), wet=wet)
        with _LOCK:
            if res.get("key") in _RESULTS:
                _RESULTS[res["key"]][slot] = sk
    return {"key": res.get("key"), "revision_cost": revision_cost(res, sk),
            "skirting": {k: sk[k] for k in ("rooms", "traces", "seconds")}}


try:
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    router = APIRouter(prefix=f"/feat/{FEATURE}", tags=[FEATURE])

    class CompareRequest(BaseModel):
        a: str
        b: str
        page_a: int = 0
        page_b: int | None = None  # None: the page of B that matches A best
        wet_skirting: bool = False  # /cost only: F4's Conventions tab includes tiled wet rooms' skirting

    class SetsRequest(BaseModel):
        a: list[str]  # issue A: the visitor's open upload + files dropped with it (each PDF page = one sheet)
        b: list[str]  # issue B: the file(s) dropped in the compare panel

    def _guard() -> None:
        if not enabled():
            raise HTTPException(404, f"feature '{FEATURE}' is off (STOA_FEATURES)")

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "feature": FEATURE, "enabled": enabled()}

    @router.post("/compare")
    def compare_route(req: CompareRequest) -> dict:
        _guard()
        try:
            return cached_compare(req.a, req.b, req.page_a, req.page_b)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/cost")
    def cost_route(req: CompareRequest) -> dict:
        """The cost of the revision (captain 21:51): quantity changes × example prices, skirting traced (slower)."""
        _guard()
        try:
            return cached_cost(req.a, req.b, req.page_a, req.page_b, wet=req.wet_skirting)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/sets")
    def sets_route(req: SetsRequest) -> dict:
        """Pair the sheets of two issues ('what changed in this issue'); the browser then compares each pair."""
        _guard()
        if not req.a or not req.b or len(req.a) + len(req.b) > 2 * SET_MAX:
            raise HTTPException(400, f"give 1-{SET_MAX} files per issue")
        try:
            return match_sets([(str(resolve(x)), x) for x in dict.fromkeys(req.a)],
                              [(str(resolve(x)), x) for x in dict.fromkeys(req.b)])
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    class NameRequest(BaseModel):
        path: str
        page: int = 0

    @router.post("/name")
    def name_route(req: NameRequest) -> dict:
        """What the sheet on screen is called (its printed title, number, revision; UX C-7: not its file name), for
        the panel before anything is compared. ~0.1 s for a PDF page; a picture only from its cached text layer."""
        _guard()
        try:
            p = resolve(req.path)
        except FileNotFoundError as e:
            raise HTTPException(404, f"no such file: {e}") from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        nm = {"a": {"name": sheet_name(p, req.page if p.suffix.lower() == ".pdf" else 0, req.path)}}
        _demo_names(nm)
        return nm["a"]["name"]

    @router.get("/image/{key}/{name}.png")
    def image_route(key: str, name: str):
        _guard()
        if not re.fullmatch(r"[0-9a-f]{16}", key) or name not in ("overlay", "b_aligned", "a"):
            raise HTTPException(400, "bad image name")
        p = CACHE_DIR / key / f"{name}.png"
        if not p.is_file():
            raise HTTPException(404, "image not rendered (compare first)")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @router.get("/demo")
    def demo_list() -> dict:
        _guard()
        out = []
        for k, d in DEMOS.items():
            ok = demo_available(k)
            out.append({"id": k, "title": d["title"], "credit": d["credit"], "available": ok,
                        "thumb": f"/feat/{FEATURE}/demo/{k}/thumb.png",
                        "a": {"name": d["a_name"], "url": f"/feat/{FEATURE}/demo/{k}/a"},
                        "b": {"name": d["b_name"], "url": f"/feat/{FEATURE}/demo/{k}/b"}})
        return {"demos": out}

    @router.get("/demo/{demo}/thumb.png")  # before /demo/{demo}/{which}: that one would take 'thumb.png' as {which}
    def demo_thumb_route(demo: str):
        """The example's picture for the catalogue card (removed red, added green, cut to where most changed)."""
        _guard()
        try:
            p = demo_thumb(demo)
        except KeyError:
            raise HTTPException(404, "no such example") from None
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from None
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @router.get("/demo/{demo}/{which}")
    def demo_file(demo: str, which: str):
        _guard()
        try:
            p = demo_path(demo, which)
        except KeyError:
            raise HTTPException(404, "no such example") from None
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from None
        return FileResponse(p, media_type="application/pdf", filename=DEMOS[demo][f"{which}_name"])
except ImportError:  # pragma: no cover  (scripts without FastAPI)
    router = None


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--page-a", type=int, default=0)
    ap.add_argument("--page-b", type=int, default=None, help="default: the page of B that matches A best")
    ap.add_argument("--out", default=None, help="write overlay.png / b_aligned.png here")
    ap.add_argument("--impact", action="store_true", help="ask the suggested questions on both (slow: pipeline)")
    ap.add_argument("--cost", action="store_true", help="price the revision incl. skirting (traces the changed rooms)")
    args = ap.parse_args(argv)
    res = compare(args.a, args.b, args.page_a, args.page_b, out_dir=args.out)
    if args.cost and res["alignment"]["ok"]:
        res["skirting"] = skirting_changes(res, args.a, args.b)
        res["revision_cost"] = revision_cost(res, res["skirting"])
    if args.impact and res["alignment"]["ok"]:
        res["impact"] = impact(res, args.a, args.b)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
