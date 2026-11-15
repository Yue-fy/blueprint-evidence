"""F8 measurement report backend: the crop / locator renderer and the sheet meta (stoa/features/report.py)."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from stoa.features import report


def _pdf(tmp_path: Path, rotate: int = 0) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=1224, height=792)
    pg.insert_text((1100, 600), "FLOOR PLAN", fontsize=10)
    pg.insert_text((1100, 760), "A-101", fontsize=24)
    pg.draw_rect(pymupdf.Rect(300, 300, 500, 450), color=(0, 0, 0), width=2)   # a room
    pg.insert_text((380, 380), "LAUNDRY", fontsize=9)
    if rotate:
        pg.set_rotation(rotate)
    p = tmp_path / f"plan{rotate}.pdf"
    doc.save(p)
    return str(p)


EV = [{"page": 0, "bbox": [300, 300, 500, 450], "note": "Laundry outline",
       "polygon": [[300, 300], [500, 300], [500, 450], [300, 450]]},
      {"page": 0, "bbox": [300, 400, 302, 430], "note": "opening 0.82 m", "polyline": [[300, 400], [300, 430]]},
      {"page": 1, "bbox": [0, 0, 5, 5], "note": "another page: not drawn here"}]


def _png(b: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(b))
    im.load()
    return im.convert("RGB")


def test_region_pads_and_clamps():
    box = report.region(EV, 0, 1224, 792, 36)
    assert box == pytest.approx((264, 264, 536, 486))          # 36 pt pad (> 18 % of 200 pt)
    assert report.region([{"page": 0, "bbox": [0, 0, 10, 10]}], 0, 1224, 792, 36)[:2] == (0, 0)
    assert report.region([], 0, 1224, 792, 36) is None


@pytest.mark.parametrize("rot", [0, 90])
def test_crop_draws_the_outline_where_the_room_is(tmp_path, rot):
    png, info = report.render(_pdf(tmp_path, rot), 0, EV, mode="crop", width=1000)
    im = _png(png)
    assert info["space"] == "pdf_pt" and info["box"] == pytest.approx([264, 264, 536, 486])
    # displayed size follows the rotation: a 272 x 222 pt box is wide upright, tall when the page is turned 90 degrees
    assert (im.width > im.height) == (rot == 0)
    # teal outline pixels exist, and the room's centre (the LAUNDRY text area) is tinted teal, not pure white
    px = im.load()
    teal = sum(1 for x in range(0, im.width, 3) for y in range(0, im.height, 3)
               if abs(px[x, y][0] - 13) < 30 and abs(px[x, y][1] - 148) < 30 and abs(px[x, y][2] - 136) < 30)
    assert teal > 50
    c = px[im.width // 2, int(im.height * 0.62) if rot == 0 else im.height // 2]
    assert c != (255, 255, 255)


def test_sheet_locator_and_image_crop(tmp_path):
    png, info = report.render(_pdf(tmp_path), 0, EV, mode="sheet", width=600)
    im = _png(png)
    assert im.width == 600 and info["box"] is not None       # whole sheet, the crop framed on it
    img = Image.new("RGB", (2000, 1400), "white")
    p = tmp_path / "scan.png"
    img.save(p)
    png, info = report.render(str(p), 0, [{"page": 0, "bbox": [900, 600, 1100, 700]}], width=800)
    assert info["space"] == "image_px" and info["box"][0] < 900 and _png(png).width == 800


def test_no_located_evidence_renders_the_whole_sheet(tmp_path):
    png, info = report.render(_pdf(tmp_path), 0, [{"page": 0, "note": "no box"}], width=500)
    assert info["box"] is None and _png(png).width == 500


def test_meta_reads_the_title_block(tmp_path):
    m = report.meta(_pdf(tmp_path), 0, shown="upload/abc/plan0.pdf")
    assert m["sheet"] == "A-101" and m["title"] == "FLOOR PLAN" and m["file"] == "plan0.pdf"
    assert m["version"] and m["generated_at"]


def test_router_off_and_on(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    up = tmp_path / "uploads" / "abc"
    up.mkdir(parents=True)
    src = Path(_pdf(tmp_path))
    (up / "plan.pdf").write_bytes(src.read_bytes())
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path / "uploads"))
    app = FastAPI()
    app.include_router(report.router)
    cl = TestClient(app)
    monkeypatch.setenv("STOA_FEATURES", "xref")
    assert cl.post("/feat/report/crop", json={"path": "upload/abc/plan.pdf", "evidence": EV}).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "all")
    r = cl.post("/feat/report/crop", json={"path": "upload/abc/plan.pdf", "page": 0, "evidence": EV})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert cl.get("/feat/report/meta", params={"path": "upload/abc/plan.pdf"}).json()["sheet"] == "A-101"
    assert cl.post("/feat/report/crop", json={"path": "../etc/passwd", "evidence": []}).status_code == 400
    assert cl.post("/feat/report/crop", json={"path": "upload/abc/nope.pdf", "evidence": []}).status_code == 404


def test_takeoff_rows_read_only_and_numbered(tmp_path, monkeypatch):
    import json

    from stoa.features import takeoff as tk

    pdf = _pdf(tmp_path)
    monkeypatch.setattr(tk, "cache_dir", lambda: tmp_path / "tk")
    assert report.takeoff_rows(pdf, 0) == {"available": False, "rows": []}     # no finished takeoff: nothing started
    assert not (tmp_path / "tk").exists()
    d = tmp_path / "tk" / tk.job_key(pdf, 0)
    d.mkdir(parents=True)
    ev = {"file": "upload/other/x.pdf", "page": 0, "bbox": [300, 300, 500, 450], "polygon": [[300, 300], [500, 300], [500, 450]]}
    rows = [{"section": "room", "item": "Laundry", "copy": 1, "copies": 1, "quantity": q, "unit": u, "value": v,
             "confidence": 0.5, "method": "lineplan", "evidence": [ev]} for q, u, v in (("area", "m2", 7.0), ("baseboard", "m", 10.1))]
    rows.append({"section": "total", "item": "Rooms labelled", "quantity": "rooms", "unit": "count", "value": 1, "evidence": []})
    (d / "result.json").write_text(json.dumps({"status": "done", "rows": rows, "scale_n": 200, "rooms": 1}))
    t = report.takeoff_rows(pdf, 0, shown="upload/mine/plan.pdf")
    assert t["available"] and [r["n"] for r in t["rows"] if r["section"] == "room"] == [1, 1]
    assert all(r["n"] is None for r in t["rows"] if r["section"] != "room")
    assert t["rows"][0]["evidence"][0]["file"] == "upload/mine/plan.pdf" and t["rows"][0]["evidence"][0]["label"] == 1
    png, info = report.render(pdf, 0, t["rows"][0]["evidence"], width=600)
    assert info["box"] is not None and abs(_png(png).width - 600) <= 2


def test_takeoff_report_matches_the_panel(tmp_path, monkeypatch):
    """UX 23:35: the printed takeoff = F2's panel (finish()): room types + colours (one palette with the panel's legend),
    the review queue, the totals without it, and the visitor's own Measure fixes."""
    import json

    from stoa.features import takeoff as tk

    pdf = _pdf(tmp_path)
    monkeypatch.setattr(tk, "cache_dir", lambda: tmp_path / "tk")
    d = tmp_path / "tk" / tk.job_key(pdf, 0)
    d.mkdir(parents=True)

    def room(item, v, conf, box):
        ev = {"file": "upload/other/x.pdf", "page": 0, "bbox": box,
              "polygon": [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]}
        return {"section": "room", "item": item, "copy": 1, "copies": 1, "quantity": "area", "unit": "m2", "value": v,
                "confidence": conf, "method": "lineplan tracer (per label)", "status": "ok", "evidence": [ev]}

    rows = [room("Laundry", 7.0, 0.8, [300, 300, 500, 450]), room("Classroom 1", 50.0, 0.8, [100, 100, 290, 290]),
            room("Store", 3.0, 0.2, [520, 300, 560, 340])]                     # rough: in review, out of the totals
    (d / "result.json").write_text(json.dumps({"status": "done", "rows": rows, "scale_n": 200, "rooms": 3}))
    t = report.takeoff_rows(pdf, 0, shown="upload/mine/plan.pdf")
    by = {r["item"]: r for r in t["rows"] if r["section"] == "room"}
    assert by["Laundry"]["room_type"] == "wet" and by["Laundry"]["type_color"] == tk.TYPE_COLOR["wet"]
    assert by["Classroom 1"]["type_label"] == tk.TYPE_LABEL["teaching"]
    assert by["Laundry"]["evidence"][0]["color"] == tk.TYPE_COLOR["wet"]
    assert by["Store"]["review"] and not by["Laundry"]["review"]
    tot = {r["quantity"]: r["value"] for r in t["rows"] if r["section"] == "total"}
    assert tot["area_sum"] == 57.0                                             # the rough Store left out, as in the panel
    assert [x["type"] for x in t["types"]][:2] == ["wet", "circulation"] and t["types"][-1]["type"] == "other"
    # the visitor measured the Store (the panel's Measure on a review row, through F2's own fix()): the report prints their
    # number; with F2's per-visitor fixes (23:52) only for THAT visitor
    import inspect

    rid = next(f"r{i}" for i, r in enumerate(rows) if r["item"] == "Store")
    per_visitor = "visitor" in inspect.signature(tk.fix).parameters
    me = "visitor-abc123" if per_visitor else None
    tk.fix(tk.job_key(pdf, 0), rid, 3.4, "m2", None, None, "Store", **({"visitor": me} if per_visitor else {}))
    t = report.takeoff_rows(pdf, 0, shown="upload/mine/plan.pdf", visitor=me)
    st = next(r for r in t["rows"] if r["item"] == "Store")
    assert st["value"] == 3.4 and st["fixed"] and not st["review"]
    assert {r["quantity"]: r["value"] for r in t["rows"] if r["section"] == "total"}["area_sum"] == 60.4
    if per_visitor:                                                            # another visitor does not see it
        other = report.takeoff_rows(pdf, 0, shown="upload/mine/plan.pdf", visitor="someone-else-42")
        assert next(r for r in other["rows"] if r["item"] == "Store")["review"]
    # the crop colours each room by its type
    png, _ = report.render(pdf, 0, [e for r in t["rows"] for e in r["evidence"]], width=800)
    im = _png(png)
    cols = {im.getpixel((x, y)) for x in range(0, im.width, 7) for y in range(0, im.height, 7)}
    # the wet room's tint = F2's own tk.TYPE_COLOR['wet'] at the report's fill alpha (70) over white paper, whatever the
    # palette is (was '#3b8fd0', '#bfdcdc' since F2 ce7b31ac: S0 / RL 00:17-00:38)
    hx = str(tk.TYPE_COLOR["wet"]).lstrip("#")
    rgb = tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))
    tint = tuple(round(255 * (1 - 70 / 255) + v * 70 / 255) for v in rgb)
    near = [c for c in cols if sum(abs(c[i] - tint[i]) for i in range(3)) <= 12]
    assert near, (tint, sorted(cols)[:12])


def test_takeoff_rows_ifc_model(tmp_path, monkeypatch):
    """F2 23:14: an IFC model's takeoff (kind 'ifc model') has rooms with GlobalId evidence (no bbox) + storey: the report
    keeps them (the table names each room's GlobalId; nothing is drawn)."""
    import json

    from stoa.features import takeoff as tk

    ifc = tmp_path / "house.ifc"
    ifc.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    monkeypatch.setattr(tk, "cache_dir", lambda: tmp_path / "tk")
    d = tmp_path / "tk" / tk.job_key(str(ifc), 0)
    d.mkdir(parents=True)
    ev = {"file": "upload/x/house.ifc", "page": None, "ifc_guid": "2O2Fr$t4X7Zf8NOew3FLKI", "note": "IfcSpace Kitchen"}
    rows = [{"section": "room", "item": "Kitchen 1 (Erdgeschoss)", "copy": 1, "copies": 1, "quantity": "area", "unit": "m2",
             "value": 12.5, "confidence": 0.95, "method": "ifc space", "storey": "Erdgeschoss", "ifc_guid": ev["ifc_guid"],
             "evidence": [ev]},
            {"section": "sheet", "item": "Whole model", "quantity": "doors", "unit": "count", "value": 5, "evidence": []}]
    (d / "result.json").write_text(json.dumps({"status": "done", "kind": "ifc model", "rows": rows}))
    t = report.takeoff_rows(str(ifc), 0, shown="upload/mine/house.ifc")
    assert t["available"] and t["kind"] == "ifc model"
    r0 = t["rows"][0]
    assert r0["n"] == 1 and r0["storey"] == "Erdgeschoss" and r0["ifc_guid"] == "2O2Fr$t4X7Zf8NOew3FLKI"
    assert r0["evidence"][0]["ifc_guid"] == "2O2Fr$t4X7Zf8NOew3FLKI" and r0["evidence"][0]["file"] == "upload/mine/house.ifc"


def test_crop_draws_a_separate_skirting_run_piece_teal_and_openings_orange(tmp_path):
    """E1's lineplan gives a long run as the outline item's own line + separate 'skirting run' items; they are the run
    (teal), not deductions. Only items whose note names an opening / door are orange (replay 00:20: 136 of 246 skirting
    answers have separate run pieces)."""
    img = Image.new("RGB", (1000, 800), "white")
    p = tmp_path / "plan.png"
    img.save(p)
    ev = [{"page": 0, "bbox": [200, 200, 800, 600], "bbox_space": "image_px", "note": "Store: skirting 11 m = outline 12 m minus 1 opening",
           "polygon": [[200, 200], [800, 200], [800, 600], [200, 600]]},
          {"page": 0, "bbox": [300, 300, 700, 300], "bbox_space": "image_px", "note": "Store: skirting run (approximate)",
           "polyline": [[300, 300], [700, 300]]},
          {"page": 0, "bbox": [300, 500, 700, 500], "bbox_space": "image_px", "note": "Store: opening 0.90 m (no skirting)",
           "polyline": [[300, 500], [700, 500]]}]
    png, info = report.render(str(p), 0, ev, mode="crop", width=1000)
    im = _png(png)
    px = im.load()
    bx = info["box"]
    sx, sy = im.width / (bx[2] - bx[0]), im.height / (bx[3] - bx[1])
    at = lambda x, y: px[int((x - bx[0]) * sx), int((y - bx[1]) * sy)]      # noqa: E731
    run, opening = at(500, 300), at(500, 500)
    assert run[1] > run[0] + 60, run              # teal-ish (green channel high, red low)
    assert opening[0] > opening[1] + 60, opening  # orange-ish (red high)


def _node() -> str | None:
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.(\d+)", v)
    return node if m and (int(m[1]), int(m[2])) >= (22, 18) else None


@pytest.mark.skipif(_node() is None, reason="needs Node >= 22.18 (type stripping)")
def test_report_geometry_check_node_tests():
    """The report's geometry check (web/src/features/report/check.ts): scale of THIS file's page, skirting runs, line width."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    test = root / "web" / "src" / "features" / "report" / "check.test.ts"
    r = subprocess.run([_node(), "--test", str(test)], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ℹ fail 0" in r.stdout
