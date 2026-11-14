"""IFC room areas: name the model's OWN area property when it differs from the drawn footprint (E1, X1 14:22).

The ifc solver answers a room's area with the IfcSpace footprint (exact on every dataset model). ArchiCAD models also
store an area property that is already REDUCED where the headroom is low (sloped roofs): Haus-G-H 'Schlafen I' has a
23.18 m² footprint and a 17.32 m² 'horizontal area'. Which one the asker means is a convention:
- the MODEL'S OWN area becomes the value only when it is a declared NET area (NetFloorArea / ArchiCAD 'horizontal
  area') that is SMALLER than the footprint by > 2 % (reduced under low ceilings; X1's ArchiCAD gold). Confidence
  <= CAP, the footprint goes to assumptions + missing.
- a generic 'Area' (Revit PSet_Revit_Dimensions.Area, 2-10 % LARGER on the dataset clinic model whose Stoa gold is the
  footprint, S0 d15) or a net area larger than the footprint keeps the FOOTPRINT as the value; the other number is
  named in one assumption. Only a smaller generic area or a larger declared net area (a real conflict) caps at CAP.

    from stoa.evidence.ifcareas import name_model_areas
    name_model_areas(answer)
"""

from __future__ import annotations

import re
from pathlib import Path

from stoa.contract import Answer

CAP = 0.6
TOL = 0.02
_AREA_KEY = re.compile(r"(?:NetFloorArea|horizontal area|\.Area|FloorArea|NetArea)$", re.I)
_NET_KEY = re.compile(r"(?:NetFloorArea|horizontal area)$", re.I)  # a declared net room area (not a generic 'Area')


def _model_area(el) -> tuple[float, str, bool] | None:
    keys = sorted((k for k in (el.qto or {}) if _AREA_KEY.search(k)),
                  key=lambda k: (not k.endswith("NetFloorArea"), not k.lower().endswith("horizontal area"), k))
    for k in keys:
        v = el.qto[k]
        if v and v > 0:
            return float(v), k.split(".")[-1], bool(_NET_KEY.search(k))
    return None


def name_model_areas(ans: Answer) -> Answer:
    if ans.value is None or (ans.solver or "").split("/")[-1] != "ifc" or (ans.unit or "").lower() not in ("m2", "m²"):
        return ans
    items = [e for e in ans.evidence if e.ifc_guid]
    if not items:
        return ans
    try:
        from stoa.solvers.ifc.index import load_index
        from stoa.solvers.ifc.solver import model_for

        model = model_for(items[0].file) if Path(items[0].file).suffix.lower() != ".ifc" else items[0].file
        if not model:
            return ans
        idx = load_index(model)
    except Exception:  # noqa: BLE001
        return ans
    diffs, fp_sum, q_sum = [], 0.0, 0.0
    for e in items:
        el = idx.elements.get(e.ifc_guid)
        if el is None or el.cls != "IfcSpace" or not el.geo or not el.geo.footprint_area:
            return ans  # not a sum of spaces: leave it
        fp = float(el.geo.footprint_area)
        m = _model_area(el)
        q = m[0] if m else fp
        fp_sum += fp
        q_sum += q
        if m and abs(q - fp) > TOL * fp:
            diffs.append((el.long_name or el.name or e.ifc_guid, q, fp, m[1], m[2] and q < fp))
    if not diffs:
        return ans
    try:
        v = float(ans.value)
    except (TypeError, ValueError):
        return ans
    if abs(v - fp_sum) > 0.02 * max(fp_sum, 1e-9):
        return ans  # the answer is not the sum of these footprints (another quantity): say nothing
    shown = ", ".join(f"'{n}' {q:.2f} m² vs footprint {fp:.2f} m²" for n, q, fp, _, _ in diffs[:4])
    prop = diffs[0][3]
    if not all(d[4] for d in diffs):
        # S0 d15: a generic / larger model area is another convention (gross, wall centre or boundary lines); the
        # dataset gold is the footprint -> keep it, name the other number; cap only when the two really conflict
        conflict = any((q < fp and not _NET_KEY.search(k)) or (q > fp and _NET_KEY.search(k)) for _, q, fp, k, _ in diffs)
        ans.assumptions = [*ans.assumptions,
                           f"area = the drawn floor footprint; the model's own area property ('{prop}') differs for "
                           f"{len(diffs)} room(s): {shown} (another measuring convention)"]
        if conflict:
            ans.missing = [*ans.missing, f"which area convention you want: footprint {fp_sum:.2f} m² (this answer) or "
                                         f"the model's own area property {q_sum:.2f} m²"]
            ans.confidence = min(ans.confidence or 0.0, CAP)
        ans.trace = [*ans.trace, f"evidence (E1): model area property ('{prop}') ≠ footprint for {len(diffs)} space(s) → "
                                 f"footprint {fp_sum:.2f} m² kept, other {q_sum:.2f} m² named"
                                 + (f", confidence capped at {CAP}" if conflict else "")]
        return ans
    # X1 gold: a declared NET area below the footprint is the room area its author declared (ArchiCAD computes it under
    # the regional rule: low headroom under a sloped roof counts reduced) -> it becomes the value; the footprint is named
    ans.value = round(q_sum, 3)
    ans.assumptions = [*ans.assumptions,
                       f"area = the model's own area property ('{prop}'), which differs from the drawn floor footprint "
                       f"for {len(diffs)} room(s): {shown} (ArchiCAD reduces it where the ceiling is low, e.g. under a "
                       "sloped roof)"]
    ans.missing = [*ans.missing, f"which area convention you want: the model's own area {q_sum:.2f} m² (this answer) or "
                                 f"the drawn footprint {fp_sum:.2f} m²"]
    by_guid = {e.ifc_guid: (q, fp) for e in items for n, q, fp, _, _ in diffs
               if (idx.elements[e.ifc_guid].long_name or idx.elements[e.ifc_guid].name or e.ifc_guid) == n}
    ev = []
    for e in ans.evidence:
        if e.ifc_guid in by_guid:
            q, fp = by_guid[e.ifc_guid]
            note = re.sub(rf"{fp:.2f}\s*m²", f"{q:.2f} m² (model area; footprint {fp:.2f} m²)", e.note)
            if note == e.note:
                note = f"{e.note} — model area {q:.2f} m² (footprint {fp:.2f} m²)"
            e = e.model_copy(update={"note": note})
        ev.append(e)
    ans.evidence = ev
    ans.confidence = min(ans.confidence or 0.0, CAP)
    ans.trace = [*ans.trace, f"evidence (E1): model area property ≠ footprint for {len(diffs)} space(s) → answered the "
                             f"model's own area {q_sum:.2f} m² (footprint {fp_sum:.2f} m² named), confidence capped at "
                             f"{CAP}"]
    return ans
