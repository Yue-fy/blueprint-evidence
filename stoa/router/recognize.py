"""INGEST step 1: recognise an uploaded drawing from its CONTENT (captain 13:33), cheap and deterministic.

recognize(path) -> {
    "kind":       "bim_plan" | "bim_elevation" | "bim_section" | "swiss_plan" | "cad_sheet" | "schedule_sheet" |
                  "cubicasa_plan" | "plan_render" | "scan" | "ifc_model" | "dxf_plan" | "svg_bim" | "pdf_scan" | "other",
    "label":      "BIM-exported floor plan",        # what the UI chip shows
    "confidence": 0.97,                             # how sure the recognition is (0..1)
    "scale":      "1:100" | None, "scale_source": "printed" | None,
    "storey":     "Level 2" | None,                 # from the title block ('Floor plan: Level 2')
    "pages":      1, "reader": "plan_rooms",        # the reader this kind is routed to
    "why":        "37 IfcSpace-style fills (#e3edf7) + 'Generated from IFC' in the title block",
}
No model call: vector style signatures for PDFs (the same ones the readers key on), the title block text, R2's image
style classifier (stoa.style) for rasters, the layer table for DXF, class="Ifc…" for SVG. ~20-200 ms per file.
Shown in the answer as the first assumption 'recognised as: <label> · <storey> · <scale> (confidence p)' and as a
trace line 'recognised: {json}' (W5 parses it for the chip + 'Change' menu).
Accuracy on dataset files labelled by their folder: experiments/g1/recognize_eval.py.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

LABELS = {
    "bim_plan": "BIM-exported floor plan", "bim_elevation": "BIM-exported elevation", "bim_section": "BIM-exported section",
    "swiss_plan": "CAD floor plan (rooms with areas)", "cad_sheet": "architectural drawing sheet (CAD/Revit)",
    "schedule_sheet": "schedule / table sheet", "cubicasa_plan": "raster floor plan (CubiCasa style)",
    "plan_render": "rendered floor plan image", "scan": "scanned / historical drawing", "ifc_model": "IFC building model",
    "dxf_plan": "DXF floor plan", "svg_bim": "BIM-exported plan (SVG)", "pdf_scan": "scanned drawing (PDF)",
    "other": "unrecognised drawing",
}
READER = {"bim_plan": "plan_rooms", "bim_elevation": "pdf_text", "bim_section": "pdf_text", "swiss_plan": "pdf_vector",
          "cad_sheet": "pdf_text", "schedule_sheet": "schedule", "cubicasa_plan": "raster", "plan_render": "raster",
          "scan": "raster", "ifc_model": "ifc", "dxf_plan": "dxf", "svg_bim": "svg_ifc", "pdf_scan": "raster",
          "other": "vlm_direct"}
_SCALE = re.compile(r"(?:Massstab|Masstab|Maßstab|Échelle|Echelle|Scala|Scale|Schaal|Skala|Mittakaava)\s*:?\s*"
                    r"(1\s*:\s*\d{1,4})|\b(1\s*:\s*(?:20|25|50|75|100|125|200|250|500|1000))\b", re.I)
_IMPERIAL = re.compile(r"(\d+/\d+|\d+(?:\.\d+)?)\s*(?:\"|”|''|in\.?)\s*=\s*1\s*'\s*-?\s*0\s*\"?", re.I)
_TITLE = re.compile(r"[—–]\s*([^:\n—–]{2,40})(?::\s*([^\n·|]{1,50}))?")  # IfcConvert-style '<project> — <view>: <name>'
_PLAN_W = re.compile(r"plan|grundriss|plattegrond|pianta|planta|pohjapiirros|rzut|plan d'étage|planritning", re.I)
_ELEV_W = re.compile(r"elevation|ansicht|façade|facade|aanzicht|gevel|prospetto|alzado|julkisivu|elewacja", re.I)
_SECT_W = re.compile(r"section|schnitt|coupe|doorsnede|snede|sezione|sección|leikkaus|przekrój", re.I)
_CUT = re.compile(r"\b1[.,]20\s*m\b")  # plan sheets cut at 1.20 m above the storey (any language)


def _rgb(c) -> tuple:
    return tuple(round(x, 2) for x in (c or ()))


@functools.lru_cache(maxsize=512)
def _pdf(path: str, mtime: float) -> dict:
    import pymupdf

    with pymupdf.open(path) as d:
        n = d.page_count
        p = d[0]
        text = p.get_text("text") or ""
        drs = p.get_drawings()
        imgs = p.get_images(full=True)
        area = p.rect.width * p.rect.height
        img_cover = 0.0
        for im in imgs[:5]:
            try:
                for r in p.get_image_rects(im[0]):
                    img_cover = max(img_cover, (r.width * r.height) / max(area, 1))
            except Exception:  # noqa: BLE001
                pass
    bim_fill = sum(1 for x in drs if _rgb(x.get("fill")) == (0.89, 0.93, 0.97))
    swiss_room = sum(1 for x in drs if x.get("type") == "fs" and _rgb(x.get("color")) == (0.54, 0.54, 0.54)
                     and round(x.get("width") or 0, 2) == 0.37)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"pages": n, "text": text, "n_draw": len(drs), "bim_fill": bim_fill, "swiss_room": swiss_room,
            "n_lines": len(lines), "img_cover": img_cover}


def _scale(text: str) -> tuple[str | None, str | None]:
    m = _SCALE.search(text or "")
    if m:
        return re.sub(r"\s+", "", m.group(1) or m.group(2)), "printed"
    m = _IMPERIAL.search(text or "")
    if m:
        return f'{m.group(1)}" = 1\'-0"', "printed"
    return None, None


def _out(kind: str, conf: float, why: str, **kw) -> dict:
    d = {"kind": kind, "label": LABELS[kind], "confidence": round(conf, 2), "scale": None, "scale_source": None,
         "storey": None, "pages": 1, "reader": READER[kind], "why": why}
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def recognize(path: str) -> dict:
    """What kind of drawing this file is, read from its content (never from its folder or name)."""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return _out("other", 0.0, "file not readable")
    try:
        if ext == ".ifc":
            with open(p, "rb") as f:
                head = f.read(4000).decode("latin-1", "ignore")
            schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", head)
            return _out("ifc_model", 0.99 if "ISO-10303-21" in head else 0.6,
                        f"STEP file, schema {schema.group(1) if schema else '?'}")
        if ext == ".dxf":
            from stoa.solvers.vector.dxf_solver import _swiss_layers

            sw = _swiss_layers(str(p))
            return _out("dxf_plan", 0.95 if sw else 0.6,
                        "layers ROOM_OUTLINE + DOOR/UNIT_OUTLINE (room polygons readable)" if sw
                        else "DXF without the room-outline layer scheme (generic CAD)")
        if ext == ".svg":
            with open(p, "rb") as f:
                head = f.read(200_000)
            if re.search(rb"""class=["'](?:cut )?Ifc""", head):
                return _out("svg_bim", 0.97, "SVG elements carry IFC classes (IfcSpace, IfcWall, …)")
            return _out("other", 0.4, "SVG without IFC classes")
        if ext == ".pdf":
            f = _pdf(str(p), mtime)
            text = f["text"]
            scale, ssrc = _scale(text)
            tm = _TITLE.search(text)
            view_w, name = (tm.group(1).strip(), (tm.group(2) or "").strip(" .") or None) if tm else ("", None)
            view = "plan" if _PLAN_W.search(view_w) else "elevation" if _ELEV_W.search(view_w) else \
                "section" if _SECT_W.search(view_w) else None
            storey = name if view == "plan" else None
            common = dict(scale=scale, scale_source=ssrc, pages=f["pages"])
            gen_ifc = "generated from ifc" in text.lower() or "ifcconvert" in text.lower()
            ifc_like = f["bim_fill"] >= 2 or (gen_ifc and f["n_draw"] > 20) or (tm is not None and scale and (
                _CUT.search(text) or view in ("elevation", "section")) and f["n_draw"] > 10)
            if ifc_like:
                if view in ("elevation", "section"):
                    return _out(f"bim_{view}", 0.9, f"IFC-export sheet style, title '{view_w}: {name}'", **common)
                return _out("bim_plan", 0.97 if f["bim_fill"] >= 2 else 0.8,
                            (f"{f['bim_fill']} IfcSpace-style room fills (#e3edf7)" if f["bim_fill"] >= 2 else
                             "IFC-export sheet layout (title '<project> — plan: <storey>', cut at 1.20 m)")
                            + (" + 'Generated from IFC' in the title block" if gen_ifc else "")
                            + ("" if f["bim_fill"] >= 2 else "; no room fills: areas need the model"),
                            storey=storey, **common)
            if f["swiss_room"] >= 3:
                return _out("swiss_plan", 0.95, f"{f['swiss_room']} room outlines in the CAD plan style (grey 0.37 pt "
                                                "filled polygons) with area labels", storey=storey, **common)
            if f["img_cover"] >= 0.6 and f["n_lines"] < 15:
                return _out("pdf_scan", 0.85, f"one image covers {100 * f['img_cover']:.0f}% of the page, almost no "
                                              "text layer", **common)
            low = text.lower()
            if re.search(r"\bschedule\b|\bzeitplan\b|\bliste\b|\btableau\b", low) and f["n_lines"] >= 60 and \
                    len(re.findall(r"^\s*[A-Z]?\d{2,4}[A-Z]?\s*$", text, re.M)) >= 8:
                return _out("schedule_sheet", 0.75, "a sheet titled …schedule… with many numbered rows", **common)
            if f["n_draw"] >= 200 and f["n_lines"] >= 20:
                return _out("cad_sheet", 0.7, f"vector drawing ({f['n_draw']} paths) with a text layer "
                                              f"({f['n_lines']} lines)", storey=storey, **common)
            return _out("other", 0.4, f"PDF with {f['n_draw']} paths and {f['n_lines']} text lines", **common)
        if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"):
            from stoa.style import guess_style

            style, pr = guess_style(str(p))
            k = {"cubicasa5k": "cubicasa_plan", "swiss_dwellings": "plan_render", "ifc_models": "plan_render",
                 "scan": "scan"}.get(style, "other")
            if k == "cubicasa_plan":
                try:
                    from stoa.pipeline import SHEET_PX, _is_upload, _sheet_sized

                    if _is_upload(str(p)) and _sheet_sized(str(p)):
                        return _out("plan_render", 0.7, f"sheet-sized image (long side > {SHEET_PX} px): a CAD sheet "
                                                        f"export, not a CubiCasa-style flat plan (style classifier "
                                                        f"said cubicasa5k p={pr:.2f})")
                except Exception:  # noqa: BLE001
                    pass
            if k == "scan" and pr < 0.85:
                # W5 19:17 / O1 19:59 / QA 18:56: Stoa's clean CAD PNG exports (M-101A, S-100F) read 'scan' 0.65-0.78
                # here, so the chip said 'scanned / historical drawing'. Pure-white paper / a clean render (raster's
                # own scan test: 1 of 160 real scans passes it) = a digital export, not a scan.
                try:
                    from stoa.solvers.raster.textqa import _clean

                    if _clean(str(p)):
                        return _out("plan_render", 0.8, f"clean digital image (CAD / PDF export: pure white paper); "
                                                        f"the style classifier's 'scan' p={pr:.2f} overruled")
                except Exception:  # noqa: BLE001 — keep the classifier's verdict
                    pass
            return _out(k, float(pr), f"image style classifier (stoa.style): {style} p={pr:.2f}")
    except Exception as e:  # noqa: BLE001 — recognition is optional
        return _out("other", 0.0, f"could not read the file ({type(e).__name__})")
    return _out("other", 0.3, f"unsupported extension {ext or '(none)'}")


def chip(rec: dict) -> str:
    """'recognised as: BIM-exported floor plan · Level 2 · 1:100 (confidence 0.97)'."""
    parts = [rec["label"]] + [x for x in (rec.get("storey"), rec.get("scale")) if x]
    if rec.get("page") is not None and rec.get("pages", 1) > 1:
        parts.append(f"page {rec['page'] + 1} of {rec['pages']}")
    elif rec.get("pages", 1) > 1:
        parts.append(f"{rec['pages']} pages")
    return f"recognised as: {' · '.join(parts)} (confidence {rec['confidence']:.2f}; {rec['why']})"


def trace_line(rec: dict) -> str:
    return "recognised: " + json.dumps({k: rec[k] for k in ("kind", "label", "confidence", "scale", "scale_source",
                                                            "storey", "pages", "page", "reader") if k in rec},
                                       ensure_ascii=False)
