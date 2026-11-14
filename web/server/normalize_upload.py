"""Make an uploaded file readable by the pipeline before the first question (called by web/mock/stoaApi.ts).

    $JUNCTION_VENV/bin/python web/server/normalize_upload.py image /tmp/stoa-uploads/ab12/IMG_2041.HEIC.webp
    $JUNCTION_VENV/bin/python web/server/normalize_upload.py dxf /tmp/stoa-uploads/ab12/floor.dxf

image: a phone photo is stored as the camera wrote it, often rotated by an EXIF tag. Browsers draw it upright, the
       pipeline reads the raw pixels, so the evidence boxes would land on the wrong spot. The photo is turned upright
       (EXIF transpose) and saved without the tag. WebP / TIFF / BMP / GIF images become PNG (the solvers read
       PNG/JPG). Very large photos are scaled down to 6000 px on the long side (a 48 MP photo takes minutes otherwise).
dxf:   a CAD file alone has nothing to look at and no sheet for the 2D solvers. A PDF sheet is drawn from it
       (ezdxf's PyMuPDF backend, white paper, black lines, text kept as text), saved under the same stem, so the
       pipeline sees the pair "plan.pdf + plan.dxf" (its dxf solver answers from the CAD entities) and the browser
       shows the sheet with the evidence boxes on it.

Prints one JSON line: {"abs": <the file to use>, "note": <what was done, or "">}. Never raises for a bad file: the
note says what went wrong and the original file is used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# the repo's own stoa/ (P1's DWG converter, DXF layer roles): not installed in the venv, so import it from this checkout
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

MAX_SIDE = 6000


def _clean(img) -> bool | None:
    """raster.windows.clean_render's verdict on a PIL image (every 2nd pixel): a clean digital render? None when the
    gate is not on this tree."""
    try:
        import numpy as np

        from stoa.solvers.raster.windows import CLEAN_TOP8, CLEAN_WHITE
    except ImportError:
        return None
    g = np.asarray(img.convert("L"))[::2, ::2]
    nw = g[g < 250]
    top = float(np.sort(np.unique(nw, return_counts=True)[1])[::-1][:8].sum() / nw.size) if nw.size else 1.0
    return float((g >= 250).mean()) >= CLEAN_WHITE and top >= CLEAN_TOP8


def image(abs_path: str) -> dict:
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = None  # P1 17:18: an A0 sheet at 400 dpi (> 179 MP) is a drawing, not a decompression bomb
    try:  # P1 17:24 (night-P1 cb0fc1d): a shrink that keeps a clean CAD export clean (flat greys), else a plain resize
        from stoa.formats.normalize import shrink_image
    except ImportError:
        shrink_image = None
    p = Path(abs_path)
    if p.suffix.lower() in (".heic", ".heif"):  # iPhone photos: readable once pillow-heif (BSD-3) is installed
        import pillow_heif  # noqa: F401  (ImportError -> the browser explains how to share the photo as JPG)

        pillow_heif.register_heif_opener()
    notes = []
    with Image.open(p) as im:
        im.load()
        orient = (im.getexif() or {}).get(0x0112, 1)
        dpi = im.info.get("dpi")  # kept (scaled with the pixels): DPI + a standard paper size = the sheet's scale (P1)
        k = 1.0
        out = ImageOps.exif_transpose(im) if orient not in (None, 1) else im
        if orient not in (None, 1):
            notes.append("turned upright (the photo's rotation tag)")
        if shrink_image and orient in (None, 1) and p.suffix.lower() in (".png", ".jpg", ".jpeg") and max(out.size) > MAX_SIDE:
            im.close()  # P1: in place is safe (it reads the image fully before writing)
            r = shrink_image(str(p), str(p), MAX_SIDE)
            return {"abs": str(r.get("path") or p), "note": str(r.get("note") or "")}
        if max(out.size) > MAX_SIDE:
            f = MAX_SIDE / max(out.size)
            k = f
            was_clean = _clean(out)
            out = out.resize((round(out.width * f), round(out.height * f)), Image.LANCZOS)
            notes.append(f"scaled to {out.width}×{out.height} px")
            # P1 17:18: LANCZOS turns a clean CAD export's 1-px lines into grey ramps, the readers then take it for a
            # SCAN (CPU OCR, minutes per question: Stoa's S-100F / M-101A). When the original was a clean render and
            # the copy is not, snap near-white to white and round the other levels to 8 steps (P1's shrink_image
            # rule; used until that function is on this tree).
            if was_clean and _clean(out) is False:
                import numpy as np

                a = np.asarray(out.convert("RGB")).astype(np.int16)
                out = Image.fromarray(np.where(a >= 240, 255, (a // 32) * 32 + 16).clip(0, 255).astype(np.uint8))
                notes.append("kept as a clean drawing (grey levels flattened after scaling)")
        ext = p.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg") and not notes:
            return {"abs": str(p), "note": ""}
        if out.mode not in ("RGB", "L", "RGBA"):
            out = out.convert("RGB")
        keep = {}
        try:
            if dpi and float(dpi[0]) > 0 and float(dpi[1]) > 0:
                keep = {"dpi": (float(dpi[0]) * k, float(dpi[1]) * k)}
        except (TypeError, ValueError, IndexError):
            keep = {}
        if ext in (".jpg", ".jpeg"):
            dst = p
            out.convert("RGB").save(dst, quality=94, **keep)
        elif ext in (".heic", ".heif"):
            dst = p.with_suffix(".jpg")
            out.convert("RGB").save(dst, quality=94, **keep)
            notes.append("iPhone photo converted to JPG")
        else:
            dst = p.with_suffix(".png")
            out.save(dst, **keep)
            if ext != ".png":
                notes.append(f"converted {ext[1:].upper()} to PNG")
    return {"abs": str(dst), "note": "; ".join(notes)}


SCALES = (20, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000)
UNIT_M = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 14: 0.1}  # $INSUNITS -> metres per drawing unit


def dxf(abs_path: str) -> dict:
    """Draw the CAD file on an A3 landscape sheet at the smallest standard scale that fits (1:50, 1:100, ...), lines
    from ezdxf's path module (blocks exploded), every TEXT/MTEXT as real PDF text centred on its insert point (so
    the PDF text layer, and the solvers' DXF-to-PDF fit on room numbers, see the same labels), and one footer line
    that says how the sheet was made."""
    import ezdxf
    import pymupdf
    from ezdxf import bbox
    from ezdxf import path as ezpath

    p = Path(abs_path)
    dst = p.with_suffix(".pdf")
    layer_note = _relayer(p)
    doc = ezdxf.readfile(str(p))
    msp = doc.modelspace()
    unit, unit_note = _units(doc, msp)
    ext = bbox.extents(msp, fast=True)
    if not ext.has_data:
        raise ValueError("the CAD file has no drawable entities")
    x0, y0, x1, y1 = ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y
    W, H, margin = 420 / 25.4 * 72, 297 / 25.4 * 72, 12 / 25.4 * 72  # A3 landscape, pt
    pt_per_m = lambda n: 1000 / n / 25.4 * 72  # noqa: E731
    n = next((k for k in SCALES if (x1 - x0) * unit * pt_per_m(k) <= W - 2 * margin
              and (y1 - y0) * unit * pt_per_m(k) <= H - 2 * margin - 14), SCALES[-1])
    s = unit * pt_per_m(n)
    a = W / 2 - s * (x0 + x1) / 2
    b = (H - 14) / 2 + s * (y0 + y1) / 2

    def to(v):
        return pymupdf.Point(s * v[0] + a, -s * v[1] + b)

    out = pymupdf.open()
    page = out.new_page(width=W, height=H)
    shape = page.new_shape()
    texts = []

    def walk(entities, depth=0):
        for e in entities:
            t = e.dxftype()
            if t in ("TEXT", "MTEXT", "ATTRIB"):
                texts.append(e)
            elif t == "INSERT" and depth < 6:
                try:
                    walk(e.virtual_entities(), depth + 1)
                except Exception:  # noqa: BLE001
                    pass
            elif t == "HATCH":
                continue  # fills only shade what the outlines already draw
            else:
                try:
                    pth = ezpath.make_path(e)
                except Exception:  # noqa: BLE001  (dimensions, images, proxies: not drawn)
                    continue
                pts = [to(v) for v in pth.flattening(0.01 / unit)] if len(pth) or pth.has_sub_paths else []
                if len(pts) >= 2:
                    shape.draw_polyline(pts)

    walk(msp)
    shape.finish(color=(0, 0, 0), width=0.5, closePath=False)
    shape.commit()
    for e in texts:
        try:
            txt = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text).strip()
        except Exception:  # noqa: BLE001
            continue
        if not txt:
            continue
        ins = e.dxf.insert
        h = float(e.dxf.get("char_height" if e.dxftype() == "MTEXT" else "height", 0.25) or 0.25)
        fs = max(4.0, min(24.0, h * s))
        for k, line in enumerate(txt.splitlines()[:6]):
            w = pymupdf.get_text_length(line, fontname="helv", fontsize=fs)
            c = to((ins[0], ins[1]))
            page.insert_text((c.x - w / 2, c.y + fs * 0.35 + k * fs * 1.15), line, fontname="helv", fontsize=fs)
    page.insert_text((margin, H - margin / 2), f"Sheet drawn from {p.name} at 1:{n} on A3 (no original sheet uploaded)",
                     fontname="helv", fontsize=7, color=(0.45, 0.45, 0.45))
    out.save(dst)
    return {"abs": str(dst), "note": f"sheet drawn from the CAD file at 1:{n}" + (f"; {unit_note}" if unit_note else "")
            + (f"; {layer_note}" if layer_note else "")}


def _units(doc, msp) -> tuple[float, str]:
    """Metres per drawing unit. $INSUNITS can lie (E1 17:18: X1's wild DXF says mm but is drawn in metres, so its sheet
    came out 1000x too small): E1's check by door-block size, else by the walls' span (stoa.evidence.dxfplan._unit),
    and without it the same span test here. Returns (unit, a note when the declared unit was overruled)."""
    declared = UNIT_M.get(int(doc.header.get("$INSUNITS", 0) or 0))
    try:
        from stoa.evidence.dxfplan import _unit  # night-E1 38086ce

        u, why = _unit(doc, msp)
    except Exception:  # noqa: BLE001 - older checkouts: the span test alone
        from ezdxf import bbox

        ext = bbox.extents(msp, fast=True)
        span = max(ext.size.x, ext.size.y) if ext.has_data else 0.0
        order = ([declared] if declared else []) + [k for k in (1.0, 0.001, 0.01, 0.0254, 0.3048) if k != declared]
        u = next((k for k in order if 4 <= span * k <= 300), declared or 1.0)
        why = f"the drawing spans {span * u:.1f} m"
    names = {0.001: "millimetres", 0.01: "centimetres", 0.1: "decimetres", 1.0: "metres", 0.0254: "inches", 0.3048: "feet"}
    if declared and abs(u / declared - 1) > 1e-6:
        return u, f"read in {names.get(u, f'{u} m')} although the file says {names.get(declared, f'{declared} m')} ({why})"
    return u, ""


def _relayer(p: Path) -> str:
    """P1 14:04: a DXF whose layers are not named like the dxf reader expects (AIA 'A-DOOR' / 'A-GLAZ', German 'Tür' /
    'Fenster', …) gets confident zeros from it. stoa.formats.dxflayers.relayer renames them to the reader's roles; the
    renamed copy becomes the sheet's DXF twin (the original is kept in .roles/). Returns one plain line for the visitor."""
    import shutil

    try:
        from stoa.formats import dxflayers
    except ImportError:  # not on this checkout yet
        return ""
    try:
        side = p.parent / ".roles"
        side.mkdir(exist_ok=True)
        got = dxflayers.relayer(p, out_dir=side)
        if got:
            shutil.copyfile(p, side / f"{p.stem}.orig.dxf")
            shutil.copyfile(got[0], p)
            pairs = [f"{k} → {v}" for k, v in list(got[1].items())[:4]]
            return "CAD layers read as: " + ", ".join(pairs) + (" …" if len(got[1]) > 4 else "")
        if not dxflayers.has_roles(p):
            return "the CAD file has no layer names we recognise (doors, windows, rooms): upload the PDF of the sheet too"
    except Exception as e:  # noqa: BLE001  (the sheet is still drawn; the reader answers as before)
        print(f"relayer: {e}", file=sys.stderr)
    return ""


def dwg(abs_path: str) -> dict:
    """DWG -> DXF next to it (P1's converter when it is on night: stoa.formats.dwg.to_dxf; else LibreDWG's dwg2dxf on
    PATH), then the DXF is drawn as a sheet like any DXF upload. No converter: an error the browser shows in words."""
    import importlib
    import shutil
    import subprocess

    p = Path(abs_path)
    out = p.with_suffix(".dxf")
    if not out.exists():
        try:
            conv = importlib.import_module("stoa.formats.dwg")
            got = conv.to_dxf(str(p))
            if got and Path(got).exists() and Path(got) != out:
                shutil.copyfile(got, out)
        except ImportError:
            exe = shutil.which("dwg2dxf")
            if not exe:
                raise RuntimeError("no DWG converter installed on this machine") from None
            subprocess.run([exe, "-y", "-o", str(out), str(p)], check=True, timeout=120, capture_output=True)
    if not out.exists():
        raise RuntimeError("the DWG could not be converted")
    r = dxf(str(out))
    r["note"] = "converted from DWG; " + r["note"]
    return r


def main() -> None:
    kind, abs_path = sys.argv[1], sys.argv[2]
    try:
        out = image(abs_path) if kind == "image" else dwg(abs_path) if kind == "dwg" else dxf(abs_path)
    except Exception as e:  # noqa: BLE001  (a file we cannot fix is still answered as uploaded)
        out = {"abs": abs_path, "note": "", "error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
