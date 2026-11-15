"""Read an upload's costly common parts BEFORE the first question (P1 17:20).

The first question on a new sheet pays for reading it: a 6000-px copy of one of Stoa's sheets = ~30 VLM text tiles =
37-50 s (P1 17:18, cold, 4 questions on one file), the 14000-px original 81 s; every later question on that sheet
takes 0.4-3 s from the caches. A visitor needs 10-30 s to type the first question, so starting the reading when the
file arrives hides most of it:

    $JUNCTION_VENV/bin/python -m stoa.formats.warm /tmp/stoa-uploads/ab12/plan.png      # prints one JSON line

`warm(path)` = normalize.prepare (DWG -> DXF, SVG -> PDF, HEIC -> JPG, a big image's smaller copy) and, for an image,
the raster text layer (VLM or OCR as raster.textlayer picks; the disk cache is keyed by the file's content, so the
question's own process finds it), the clean-render gate, the recognise step, and for a clean sheet whose paper is
known (DPI tag, or pixel size + the sheet size it prints) the traced vector copy upload._as_vector reads (Stoa's DfE
PNG at 6000 px: the first question 158 s cold with tracing, later ones 3-9 s). A one-page PDF needs nothing: its
own text layer is read in milliseconds; one whose text is drawn as outlines gets its page render and that render's
text layer (OCR) here, as the first question would. Multi-page PDFs need the question to pick a page; a
SCANNED set (every page a picture, <= pages.SCAN_TEXT_PAGES) gets every page's text read here, for that pick.
A question on the same file waits for a running warm (wait_for_warm, <= STOA_WARM_WAIT), but not for its trace:
that runs after the lock (the question's own wait for it is STOA_TRACE_WAIT). Never raises.
Run as a command it is PRIVATE (STOA_PRIVATE=1: no hosted model; STOA_WARM_HOSTED=1 to allow them).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg"}
WAIT_S = float(os.environ.get("STOA_WARM_WAIT", "240"))  # how long a question waits for a running warm of its file
GRACE_S = float(os.environ.get("STOA_WARM_GRACE", "3"))  # a just-uploaded file: wait this long for its warm to start


def _content_key(path: str | Path) -> str:
    """Size + first and last 64 KB of the file: the same bytes under another name / folder give the same key."""
    import hashlib

    p = Path(path)
    st = p.stat()
    h = hashlib.sha1(str(st.st_size).encode())
    with open(p, "rb") as f:
        h.update(f.read(65536))
        if st.st_size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()[:20]


def _lock_path(path: str | Path, create: bool = True) -> Path:
    """One lock per file CONTENT in a shared folder ($STOA_CACHE/warm). QA 20:19: no question ever waited for its
    pre-read: the UI warms the plain upload ('upload/<id>/<name>') but asks about a style-tagged copy of the same bytes
    in another folder ('upload/<id>/<style>/<name>'), and the old lock lived next to the file, so the question read
    the sheet a second time in parallel. The waiter never creates the folder (dataset files: no litter per question)."""
    root = Path(os.environ.get("STOA_CACHE", Path.home() / ".cache" / "stoa")) / "warm"
    try:
        key = _content_key(path)
    except OSError:  # unreadable: a per-path key (the warm will fail on it anyway)
        import hashlib

        key = "p" + hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()[:19]
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root / f"{key}.warm.lock"


@contextlib.contextmanager
def _held(path: str | Path):
    """Exclusive per-file lock while warm() reads the file (released by the kernel if the process dies)."""
    f = None
    try:
        f = open(_lock_path(path), "w")
        fcntl.flock(f, fcntl.LOCK_EX)
    except OSError:  # read-only cache / no locks: warm without one
        f = None
    try:
        yield
    finally:
        if f is not None:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


def wait_for_warm(path: str | Path, timeout: float | None = None) -> float:
    """Seconds spent waiting for a warm() of this file that is still running in another process or thread (0.0 when
    none). P1 18:3x: the UI starts warm when an image arrives; a question typed before it finishes used to read the
    same sheet AGAIN in parallel (two VLM text-layer reads + two traces on the shared GPU / CPU), so neither got
    faster. Waiting, then reading warm's caches, is never slower. Bounded by STOA_WARM_WAIT (240 s)."""
    timeout = WAIT_S if timeout is None else timeout
    t0 = time.monotonic()
    try:
        lp = _lock_path(path, create=False)
        if timeout <= 0:
            return 0.0
        if not lp.exists():
            # a file written seconds ago: its warm process may not hold the lock yet (QA 20:19: a question asked right
            # after the upload never waited). Give it up to GRACE_S to appear; an old file has no warm coming.
            try:
                young = time.time() - Path(path).stat().st_mtime < 10
            except OSError:
                young = False
            while young and not lp.exists() and time.monotonic() - t0 < GRACE_S:
                time.sleep(0.2)
            if not lp.exists():
                return 0.0
        f = open(lp, "a")
    except OSError:
        return 0.0
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                break
            except BlockingIOError:
                if time.monotonic() - t0 >= timeout:
                    break
                time.sleep(0.25)
            except OSError:
                break
    finally:
        f.close()
    return round(time.monotonic() - t0, 2)


def _outlined(pdf: str, min_chars: int = 20) -> bool:
    """One-page PDF whose own text layer is missing or junk (the cases normalize.seed_text_layer leaves to OCR)."""
    import pymupdf

    try:
        with pymupdf.open(pdf) as d:
            if d.page_count != 1:
                return False
            raw = d[0].get_text() or ""
    except Exception:  # noqa: BLE001
        return False
    if len(raw.strip()) < min_chars:
        return True
    return sum(ch.isprintable() or ch in "\n\t" for ch in raw) < 0.97 * len(raw)


def warm(path: str) -> dict:
    # P1 00:40: the TRACE runs after the per-file lock is released. A question waits for warm (wait_for_warm, <= 240 s)
    # and the trace used to sit inside it: Stoa-size scans trace 47-132 s at load, so the first question waited for the
    # text layer + the whole trace + its own 30 s readers. Now it waits for the text layer only; upload._as_vector
    # waits for the running trace up to STOA_TRACE_WAIT (traced_copy's own lock: still one trace per sheet).
    t0 = time.perf_counter()
    later: list = []
    with _held(path):
        out = _warm(path, later)
    for label, fn in later:
        t1 = time.perf_counter()
        try:
            fn()
            out["done"].append(f"{label}, {time.perf_counter() - t1:.1f} s)")
        except Exception as e:  # noqa: BLE001 — warming is only a head start; the question traces it itself
            out["error"] = out.get("error") or f"{type(e).__name__}: {e}"
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def _warm(path: str, later: list | None = None) -> dict:
    """The locked part of warm(); steps a question need not wait for go to `later` as (label, fn), run unlocked."""
    t0 = time.perf_counter()
    out: dict = {"path": str(path), "done": [], "error": ""}

    def defer(label: str, fn) -> None:
        if later is None:
            t1 = time.perf_counter()
            fn()
            out["done"].append(f"{label}, {time.perf_counter() - t1:.1f} s)")
        else:
            later.append((label, fn))
    try:
        from stoa.formats.normalize import prepare

        prep = prepare(str(path), "")
        out["solved_file"], out["kind"] = prep.path, prep.kind
        if prep.kind != "as_is":
            out["done"].append(f"prepared ({prep.kind})")
        if Path(str(path)).suffix.lower() == ".pdf" and (getattr(prep, "pages", 0) or 0) > 1:
            # captain 18:50: a SCANNED set (8 floor plans as pictures): read every page's text now, so the first
            # question's page picking can match its room / storey names (pages.read_scan_texts; one text layer per
            # page, the same files a question on that page reads) and say per page which floor / kind it is
            from stoa.formats import pages as PG

            if PG.scanned_set(PG.page_scores(path)):
                t1 = time.perf_counter()
                texts = PG.read_scan_texts(path)
                out["pages"] = [{"page": k + 1, "storey": PG.sheet_storey(t or ""), "kind": PG._sheet_kind(t or ""),
                                 "texts": len((t or "").splitlines())} for k, t in sorted(texts.items())]
                out["done"].append(f"scanned set: text of {sum(1 for t in texts.values() if t)} of {prep.pages} pages "
                                   f"({time.perf_counter() - t1:.1f} s)")
        if Path(prep.path).suffix.lower() in IMAGE_EXT:
            from stoa.solvers.raster import textlayer as T

            out["clean"] = T.is_clean(prep.path)
            tl = T.text_layer(prep.path)
            out["texts"] = len(tl.items)
            out["done"].append(f"text layer ({len(tl.items)} texts, {time.perf_counter() - t0:.1f} s)")
            if out["clean"]:  # a clean sheet export with a known paper: the vector copy that upload._as_vector reads
                from stoa.formats import vectorize as V
                from stoa.formats.paper import paper_of_image

                hit = V.paper(prep.path) or paper_of_image(prep.path, [i.text for i in tl.items])
                if hit:
                    defer(f"vector copy ({hit[1]} at {hit[0]:.0f} dpi", lambda p=prep.path, d=hit[0]: V.traced_copy(p, d))
            elif getattr(prep, "scan_zoom", 0) and getattr(prep, "scan_pdf", ""):
                # P1 22:50: a scanned PDF page whose paper is known (its own pixels + printed size, its scale bar, or its
                # page size): trace it now, so the first area / length question does not pay the trace (+60-70 s at load
                # ~130 in the d29 scan A/B); upload.trace_paper = the same paper / dpi the question's _as_vector asks for
                from stoa.formats import vectorize as V
                from stoa.solvers.upload import trace_paper

                hit, how = trace_paper(prep)
                if hit:
                    defer(f"vector copy of the scan ({hit[1]} at {hit[0]:.0f} dpi {how}",
                          lambda p=prep.path, d=hit[0]: V.traced_copy(p, d))
        elif Path(prep.path).suffix.lower() == ".pdf" and prep.kind in ("as_is", "paper") and _outlined(prep.path):
            # captain 18:21 (7): a one-page sheet whose text is drawn as outlines has no words for the readers; the
            # first question renders the page and OCRs it on CPU (D1's AW-29: 1,148 s at load ~200). Do that render and
            # its text layer now; the question finds both cached (same file, same content key) or waits for this.
            from stoa.formats.normalize import render_png
            from stoa.solvers.raster import textlayer as T

            t1 = time.perf_counter()
            png, _zoom = render_png(prep.path)
            out["render"] = png
            out["clean"] = T.is_clean(png)
            tl = T.text_layer(png)
            out["texts"] = len(tl.items)
            out["done"].append(f"page render + text layer of an outlined-text sheet ({len(tl.items)} texts, "
                               f"{time.perf_counter() - t1:.1f} s)")
        try:
            from stoa.router.recognize import recognize

            t1 = time.perf_counter()
            rec = recognize(prep.path)
            out["recognised"] = getattr(rec, "kind", None) or (rec.get("kind") if isinstance(rec, dict) else None)
            out["done"].append(f"recognised ({time.perf_counter() - t1:.1f} s)")
        except Exception as e:  # noqa: BLE001 — older trees / unreadable: the question does it itself
            out.setdefault("notes", []).append(f"recognise skipped ({type(e).__name__})")
    except Exception as e:  # noqa: BLE001 — warming is only a head start; the question still answers
        out["error"] = f"{type(e).__name__}: {e}"
    out["seconds"] = round(time.perf_counter() - t0, 2)
    return out


def main(argv: list[str]) -> None:
    # RL 19:21 (captain 18:53 private mode): the pre-read runs per UPLOAD, before anyone chose Private on or off, so it
    # never sends the drawing to a hosted model (stoa.llm refuses one under STOA_PRIVATE=1; nothing is cached for a
    # step it refuses). On our GPU servers this changes nothing (every step is local); with STOA_MODE=api the hosted
    # steps wait for a question that allows them. STOA_WARM_HOSTED=1 lets the pre-read use them anyway.
    if os.environ.get("STOA_WARM_HOSTED", "").strip() != "1":
        os.environ["STOA_PRIVATE"] = "1"
    for p in argv or []:
        print(json.dumps(warm(p)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
