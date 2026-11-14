"""Ground-truth object boxes per drawing, in the evidence's own coordinate space (E1).

Sources with exact geometry next to the drawing:
- `ifc_models` plan sheets (`<model>_plan-NN-<level>.pdf`): the `.svg` twin is the same IfcConvert render, one
  `<g data-guid=… class="cut IfcDoor">` per element, so every drawn element has an exact box (stoa.solvers.ifc.plan2d).
- `swiss_dwellings` (`.pdf` / `.png` with a `.dxf` twin): DXF rooms, apartments, doors, windows and fixtures, mapped
  onto the PDF with the similarity transform fitted on room-number texts (stoa.solvers.vector.pdfmap; RMS is
  reported per drawing, ~0.05 pt) and onto the PNG by its fixed render scale.
Anything else (cubicasa, scans, schedules, uploads) has no ground truth here: `objects_for` returns [].

`kind` is normalised across sources: door, window, room, apartment, wall, column, beam, slab, stair, railing, wc,
washbasin, bathtub, shower, sanitary, furniture, light, storey, member, other.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

from stoa.data import abspath

Box = tuple[float, float, float, float]
TOL_PT = 2.0  # 2 pt = 0.7 mm on paper (7 cm at 1:100): boxes are compared after padding both by this much

_IFC_KIND = {
    "IfcDoor": "door", "IfcDoorStandardCase": "door", "IfcWindow": "window", "IfcWindowStandardCase": "window",
    "IfcSpace": "room", "IfcWall": "wall", "IfcWallStandardCase": "wall", "IfcCurtainWall": "wall",
    "IfcColumn": "column", "IfcColumnStandardCase": "column", "IfcBeam": "beam", "IfcBeamStandardCase": "beam",
    "IfcSlab": "slab", "IfcSlabStandardCase": "slab", "IfcRoof": "slab", "IfcCovering": "slab",
    "IfcStair": "stair", "IfcStairFlight": "stair", "IfcRamp": "stair", "IfcRampFlight": "stair",
    "IfcRailing": "railing", "IfcSanitaryTerminal": "sanitary", "IfcFlowTerminal": "sanitary",
    "IfcFurnishingElement": "furniture", "IfcFurniture": "furniture", "IfcLightFixture": "light",
    "IfcBuildingStorey": "storey", "IfcMember": "member", "IfcPlate": "member",
}
_DXF_KIND = {"door": "door", "window": "window", "toilet": "wc", "washbasin": "washbasin", "bathtub": "bathtub",
             "shower": "shower", "sink": "washbasin", "kitchen": "washbasin"}


def ifc_kind(cls: str | None) -> str:
    if not cls:
        return "other"
    return _IFC_KIND.get(cls, "other")


@dataclass
class GTObject:
    kind: str
    box: Box  # in the requested space (pdf_pt or image_px)
    ident: str  # GUID for IFC, "door 3" / "room 2.01" for DXF
    label: str = ""  # room name / IFC data-name
    polygon: list[tuple[float, float]] | None = None  # rooms/spaces: outline in the same space
    sub_boxes: list[Box] = field(default_factory=list)  # extra boxes that belong to the object (room label block)


@dataclass
class GroundTruth:
    source: str  # "ifc_svg" | "swiss_dxf"
    objects: list[GTObject]
    by_ident: dict[str, GTObject]
    quality: str = ""  # e.g. "fit RMS 0.04 pt on 12 anchors"
    tol: float = TOL_PT  # match tolerance in this space (2 pt on paper, scaled for image_px)


def _svg_twin(path: str) -> str | None:
    p = Path(path)
    if p.suffix.lower() not in (".pdf", ".svg"):
        return None
    svg = p.with_suffix(".svg")
    return str(svg) if abspath(str(svg)).exists() else None


def _dxf_twin(path: str) -> str | None:
    p = Path(path)
    if p.suffix.lower() not in (".pdf", ".png"):
        return None
    dxf = p.with_suffix(".dxf")
    return str(dxf) if abspath(str(dxf)).exists() else None


@functools.lru_cache(maxsize=512)
def ground_truth(path: str, space: str = "pdf_pt") -> GroundTruth | None:
    """Ground truth for a dataset-relative drawing in `space`; None when there is none (or it can't be built)."""
    try:
        if space == "dxf" and Path(path).suffix.lower() == ".dxf":
            return _from_dxf_units(path)
        if space == "svg" and Path(path).suffix.lower() == ".svg":
            return _from_svg_units(path)
        if _svg_twin(path) and space == "pdf_pt":
            return _from_ifc_svg(path)
        if _dxf_twin(path):
            return _from_swiss_dxf(path, space)
    except Exception:  # noqa: BLE001 — a broken twin means "no ground truth", never a crash in eval
        return None
    return None


def objects_for(path: str, page: int | None = 0, space: str = "pdf_pt") -> list[GTObject]:
    if page not in (0, None):
        return []  # every drawing with ground truth here is a one-page PDF / one image
    gt = ground_truth(path, space)
    return gt.objects if gt else []


def _from_ifc_svg(path: str) -> GroundTruth | None:
    from stoa.solvers.ifc.plan2d import plan_items

    items = plan_items(str(Path(path).with_suffix(".pdf")))
    if not items:
        return None
    objs = []
    for guid, it in items.items():
        objs.append(GTObject(kind=ifc_kind(it.cls), box=tuple(it.bbox), ident=guid, label=it.label or it.name or "",
                             polygon=list(it.polygon) if it.polygon else None,
                             sub_boxes=[tuple(it.label_bbox)] if it.label_bbox else []))
    return GroundTruth("ifc_svg", objs, {o.ident: o for o in objs}, quality="exact (SVG twin, same render)")


def _from_swiss_dxf(path: str, space: str) -> GroundTruth | None:
    from stoa.solvers.vector.floor import load_floor
    from stoa.solvers.vector.pdfmap import fit_transform, png_scale

    dxf = _dxf_twin(path)
    pdf = str(Path(path).with_suffix(".pdf"))
    if not abspath(pdf).exists():
        return None
    tf = fit_transform(str(abspath(pdf)), str(abspath(dxf)))
    if tf is None:
        return None
    k = 1.0
    if space == "image_px":
        if Path(path).suffix.lower() != ".png":
            return None
        k = png_scale(str(abspath(path)), tf.page_w)
    elif Path(path).suffix.lower() != ".pdf":
        return None

    def box(bb) -> Box:
        b = tf.box(bb)
        return (b[0] * k, b[1] * k, b[2] * k, b[3] * k)

    def pt(x, y):
        px, py = tf.pt(x, y)
        return (px * k, py * k)

    fm = load_floor(str(abspath(dxf)))
    objs: list[GTObject] = []
    for r in fm.rooms:
        poly = [pt(x, y) for x, y in r.poly.exterior.coords] if r.poly is not None else None
        subs = [box(t.bbox) for t in r.texts]
        objs.append(GTObject("room", box(r.bbox), f"room {r.number or r.idx}", r.name or "", poly, subs))
    for i, u in enumerate(fm.units):
        poly = [pt(x, y) for x, y in u.poly.exterior.coords] if u.poly is not None else None
        objs.append(GTObject("apartment", box(u.bbox), f"apartment {u.label or i}", u.label or "", poly))
    for kind, items in fm.items.items():
        for i, it in enumerate(items):
            objs.append(GTObject(_DXF_KIND.get(kind, kind), box(it.bbox), f"{kind} {i + 1}", it.note))
    q = f"fit RMS {tf.rms_pt:.2f} pt on {tf.n_anchors} room-text anchors"
    return GroundTruth("swiss_dxf", objs, {o.ident: o for o in objs}, quality=q, tol=TOL_PT * k)


def _from_dxf_units(dxf: str) -> GroundTruth | None:
    """D29: a lone DXF's objects in its own drawing units (y up), for evidence in bbox_space 'dxf'."""
    from stoa.solvers.vector.floor import load_floor

    fm = load_floor(str(abspath(dxf)))
    objs: list[GTObject] = []
    for r in fm.rooms:
        poly = [(x, y) for x, y in r.poly.exterior.coords] if r.poly is not None else None
        objs.append(GTObject("room", tuple(r.bbox), f"room {r.number or r.idx}", r.name or "", poly,
                             [tuple(t.bbox) for t in r.texts]))
    for i, u in enumerate(fm.units):
        poly = [(x, y) for x, y in u.poly.exterior.coords] if u.poly is not None else None
        objs.append(GTObject("apartment", tuple(u.bbox), f"apartment {u.label or i}", u.label or "", poly))
    for kind, items in fm.items.items():
        for i, it in enumerate(items):
            objs.append(GTObject(_DXF_KIND.get(kind, kind), tuple(it.bbox), f"{kind} {i + 1}", it.note))
    return GroundTruth("dxf_units", objs, {o.ident: o for o in objs}, quality="exact (the DXF itself)", tol=0.05)


def _from_svg_units(svg: str) -> GroundTruth | None:
    """D29: a lone IfcConvert SVG's elements in its own user units (y down), for evidence in bbox_space 'svg'."""
    from stoa.evidence.plan_svg import _spaces, read_svg

    d = read_svg(str(abspath(svg)))
    if not d:
        return None
    polys = {s["guid"]: [tuple(p) for p in s["ring"]] for s in _spaces(d)}
    objs = [GTObject(ifc_kind(e["cls"]), tuple(e["bbox"]), e["guid"], e["name"], polys.get(e["guid"]))
            for e in d["elements"]]
    return GroundTruth("svg_units", objs, {o.ident: o for o in objs}, quality="exact (the SVG itself)", tol=0.7)
