"""`Evidence.exact` (D25): True = the box comes from the drawing's own geometry, False = estimated, None = unknown.

    from stoa.evidence.exactness import mark_exact
    mark_exact(answer)   # fills exact where a solver left it None; never overrides a solver's own value

True  — CAD/IFC geometry and text read from a vector file: dxf (DXF entities, transform fitted on room-number texts,
        ~0.05 pt RMS), ifc plan boxes from the SVG twin, schedule rows, pdf_vector / pdf_text text and paths, and the
        filled room polygons plan_rooms / ifcplan_pdf measure on a lone vector PDF (the box is exact even when the
        scale is guessed: the scale is stated separately, in assumptions).
False — image recognition or a fitted/projected place: CubiCasa detector boxes, segmentation regions, door swings,
        VLM/LoRA boxes, model-footprint boxes the plan does not draw ("projected: not drawn"), E1 candidates.
None  — anything else (agent tool output we can't attribute, file-level items without a box or GUID).
The same note rule drives the web UI's dashed "approximate" boxes (web/src/customer.ts approxEvidence) and the
evidence-precision report (stoa/eval/evidence.py).
"""

from __future__ import annotations

import re

from stoa.contract import Answer

APPROX_NOTE = re.compile(r"segmentation|detector|room graph|door[- ]?swing|flood|estimat|approx|projected: not drawn|"
                         r"~\s*\d|\bregion R\d|candidate", re.I)
APPROX_SOLVER = re.compile(r"raster_cc5k|vlm|raster\b|prior", re.I)
EXACT_SOLVER = re.compile(r"(?:^|/)(?:dxf|ifc|schedule|pdf_vector|pdf_text|plan_rooms|ifcplan_pdf|plan_svg|svg_ifc)$", re.I)


def classify(note: str, solver: str, has_box: bool, has_guid: bool) -> bool | None:
    if not (has_box or has_guid):
        return None
    note = note or ""
    # (no 'label reader:' exception: the raster text layer is VLM text-spotting, whose boxes are guesses (O1 12:37);
    # an OCR engine with detector polygons sets exact=True itself, and mark_exact never overrides a solver's value)
    if APPROX_NOTE.search(note):
        return False
    if has_box and APPROX_SOLVER.search(solver or ""):
        return False
    if EXACT_SOLVER.search(solver or ""):
        return True
    if has_guid and not has_box:
        return True  # a GlobalId names the element itself
    return None


def mark_exact(ans: Answer) -> Answer:
    if not ans.evidence:
        return ans
    out, changed = [], False
    for e in ans.evidence:
        if e.exact is None:
            x = classify(e.note, ans.solver, bool(e.bbox), bool(e.ifc_guid))
            if x is not None:
                e = e.model_copy(update={"exact": x})
                changed = True
        out.append(e)
    if changed:
        ans.evidence = out
    return ans
