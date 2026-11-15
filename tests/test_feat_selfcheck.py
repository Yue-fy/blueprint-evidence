"""F3 drawing self-check (stoa.features.selfcheck): synthetic sheets with known slips, and Stoa's own sheets when the
shared stoa-like copy is on this machine (clean = no findings; seeded copies = exactly the planted slip)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from stoa.features import enabled, include_feature_routers
from stoa.features import selfcheck as SC

REPO = Path(__file__).resolve().parents[1]
ORIG = Path(".stoa/datasets/stoa-like/originals")


def _table_pdf(tmp_path: Path, rows: list[tuple[str, str]]) -> str:
    """A one-page PDF with a printed area table 'LABEL = value SF', one row per line, labels and values apart."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=800)
    y = 100
    for label, val in rows:
        pg.insert_text((60, y), f"{label} =", fontsize=9, fontname="helv")
        pg.insert_text((300, y), f"{val} SF", fontsize=9, fontname="helv")
        y += 16
    out = tmp_path / "table.pdf"
    doc.save(out)
    return str(out)


AFCEC_ROWS = [("WEST WING DORMITORY", "6,095"), ("WEST WING CIRCULATION", "914"), ("TOTAL WEST WING", "7,009"),
              ("EAST WING DORMITORY", "6,092"), ("EAST WING CIRCULATION", "913"), ("TOTAL EAST WING", "7,005"),
              ("SUBTOTAL", "14,014"), ("EXTERIOR STAIR", "381"), ("TOTAL SECOND FLOOR", "14,395")]


def test_area_table_that_adds_up_has_no_finding(tmp_path):
    rep = SC.check(_table_pdf(tmp_path, AFCEC_ROWS), 0, "t.pdf", checks=["area_table_sums"])
    assert rep["findings"] == []
    assert len(rep["passed"]) == 4  # two wings, the subtotal, the floor total
    assert rep["cost"]["api_calls"] == 0 and rep["cost_tier"] == "cheap"


def test_area_table_row_slip_is_reported_on_its_own_total_only(tmp_path):
    rows = list(AFCEC_ROWS)
    rows[1] = ("WEST WING CIRCULATION", "941")  # 6,095 + 941 = 7,036 != 7,009
    rep = SC.check(_table_pdf(tmp_path, rows), 0, "t.pdf", checks=["area_table_sums"])
    assert len(rep["findings"]) == 1
    f = rep["findings"][0]
    assert f["check"] == "area_table_sums" and "TOTAL WEST WING" in f["title"]
    assert {r["value"] for r in f["readings"]} == {7009.0, 7036.0}
    assert all(e["bbox_space"] == "pdf_pt" and e["page"] == 0 for e in f["evidence"])
    assert f["zoom"] and f["id"] == "F1"


def test_area_table_final_total_slip_uses_the_open_stack(tmp_path):
    rows = list(AFCEC_ROWS)
    rows[-1] = ("TOTAL SECOND FLOOR", "14,495")  # SUBTOTAL 14,014 + 381 = 14,395
    rep = SC.check(_table_pdf(tmp_path, rows), 0, "t.pdf", checks=["area_table_sums"])
    assert [f["title"] for f in rep["findings"]] == ["TOTAL SECOND FLOOR does not add up"]


def test_area_table_total_that_leaves_rows_out_is_not_an_alarm(tmp_path):
    # 'TOTAL DORMITORY' sums only the dormitory rows (+ a far-off grouping): no suffix / qualifier match -> no finding
    rows = [("WEST WING DORMITORY", "6,095"), ("WEST WING CIRCULATION", "914"), ("EAST WING DORMITORY", "6,092"),
            ("EAST WING CIRCULATION", "913"), ("TOTAL DORMITORY", "12,187")]
    rep = SC.check(_table_pdf(tmp_path, rows), 0, "t.pdf", checks=["area_table_sums"])
    assert rep["findings"] == []


def test_reading_order_sorts_rows_drawn_out_of_order():
    rows = [("TOTAL A", 3.0, "ft2", (10, 140, 200, 150)), ("A1", 1.0, "ft2", (10, 100, 200, 110)),
            ("A2", 2.0, "ft2", (12, 120, 200, 130))]
    assert [r[0] for r in SC._reading_order(rows)] == ["A1", "A2", "TOTAL A"]


def test_summary_line_and_never_raises(tmp_path):
    rep = SC.check(str(tmp_path / "missing.pdf"), 0)
    assert rep["findings"] == [] and all(c["status"] in ("skipped", "failed") for c in rep["checks"])
    assert SC.summary_line(rep).startswith("Nothing to cross-check")
    assert SC.summary_line({"findings": [{}], "passed": [{}, {}]}) == "1 issue found · 2 cross-checks agree"


def test_feature_flag_mounts_only_when_named():
    fastapi = pytest.importorskip("fastapi")
    assert enabled("") == [] and enabled("selfcheck") == ["selfcheck"] and "selfcheck" in enabled("all")
    app = fastapi.FastAPI()
    assert include_feature_routers(app, "") == []
    assert include_feature_routers(app, "selfcheck,unknown") == ["selfcheck"]
    assert [r.path for r in SC.router.routes] == ["/feat/selfcheck/run", "/feat/selfcheck/copy", "/feat/selfcheck/checks"]
    try:
        from fastapi.testclient import TestClient
    except Exception:  # noqa: BLE001  (no httpx)
        return
    js = TestClient(app).get("/feat/selfcheck/checks").json()
    assert js["version"] == SC.VERSION and len(js["checks"]) == len(SC.ALL_CHECKS)


def test_router_resolves_only_allowed_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    up = tmp_path / "ab12" / "plan.pdf"
    up.parent.mkdir()
    up.write_bytes(b"%PDF-1.4\n")
    assert SC.resolve("upload/ab12/plan.pdf") == up.resolve()
    assert SC.resolve("upload/../etc/passwd") is None
    assert SC.resolve("/etc/passwd") is None
    # a style-tagged picture: browser path upload/<id>/<style>/<name>, stored at <uploads>/<id>/data/<style>/<name>
    pic = tmp_path / "0123456789ab" / "data" / "cubicasa5k" / "F1_original.png"
    pic.parent.mkdir(parents=True)
    pic.write_bytes(b"\x89PNG\r\n")
    assert SC.resolve("upload/0123456789ab/cubicasa5k/F1_original.png") == pic.resolve()
    assert SC.resolve("upload/0123456789ab/cubicasa5k/../../../x.png") is None


# ---- Stoa's sheets (shared copy; skipped on fresh clones) -------------------------------------------------------------

def _need(p: Path) -> str:
    if not p.is_file():
        pytest.skip(f"needs the shared stoa-like copy: {p}")
    return str(p)


def test_stoa_dimension_chains_add_up_on_s100f():
    rep = SC.check(_need(ORIG / "usace-S-100F-foundation-slab-plan.pdf"), 0,
                   checks=["dimension_chains", "dimension_drawn"])
    assert rep["findings"] == []  # the '1750' the reader measures 1,588 mm is 1,732 mm between its extension lines
    assert sum(1 for p in rep["passed"] if p["check"] == "dimension_chains") >= 6


@pytest.mark.parametrize("old,new,check,expect", [
    ("4635", "4365", "dimension_chains", "4365"),   # a chain piece, read by the reader on its arrow bases
    ("9800", "9890", "dimension_chains", "9890"),   # the overall number
])
def test_stoa_seeded_dimension_slip_on_a101f(tmp_path, old, new, check, expect):
    src = _need(ORIG / "usace-A-101F-filter-building-floor-plan.pdf")
    out = tmp_path / "seeded.pdf"
    r = subprocess.run([sys.executable, _release_workspace_script(REPO / "experiments/f3/seed.py"), src, "0", old, new, str(out)],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert r.returncode == 0, r.stderr
    rep = SC.check(str(out), 0, checks=["dimension_chains", "dimension_drawn"])
    assert len(rep["findings"]) == 1
    f = rep["findings"][0]
    assert f["check"] == check and expect in f["title"]


def test_feet_inch_dimension_text():
    assert SC._ftin_mm("133'-10\"") == round((133 * 12 + 10) * 25.4)
    assert SC._ftin_mm("2'-6 1/2\"") == round((30.5) * 25.4)
    assert SC._ftin_mm("40'") is None and SC._ftin_mm("1/8\" = 1'-0\"") is None  # bare feet / scale notes
    assert SC._ftin_txt(SC._ftin_mm("323'-4\"")) == "323'-4\""


def test_stoa_feet_inch_chains_on_afcec():
    rep = SC.check(_need(ORIG / "afcec-A-2-second-floor-plan-T-shape.pdf"), 0, checks=["dimension_chains"])
    assert rep["findings"] == []
    assert any("177'-4\" + 108'-8\" = 286'-0\"" in p["what"] for p in rep["passed"])


def test_stoa_door_schedule_vs_plan_tags(tmp_path):
    """AW 078-24-33 (121 sheets): the door schedule (p76) marks are all tagged on its plans; a copy whose plan tag
    104B is retyped 104C gives exactly 'door 104B not on any plan' + '104C tagged but not scheduled'."""
    src = _need(ORIG / "AW_078-24-33.pdf")
    rep = SC.check(src, 68, checks=["schedule_tags"])
    assert rep["findings"] == [] and any("all 8 marks" in p["what"] for p in rep["passed"])
    out = tmp_path / "seeded.pdf"
    r = subprocess.run([sys.executable, _release_workspace_script(REPO / "experiments/f3/seed.py"), src, "68", "104B", "104C", str(out)],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert r.returncode == 0, r.stderr
    rep = SC.check(str(out), 68, checks=["schedule_tags"])
    titles = sorted(f["title"] for f in rep["findings"])
    assert titles == ["Door 104B not on any plan", "Door 104C is tagged but not scheduled"]
    assert {e["page"] for f in rep["findings"] for e in f["evidence"]} == {68, 75}


def test_scale_bar_is_not_compared_on_pictures(tmp_path):
    # a bar on an image measures px per metre, not a paper 1:N (Stoa's DfE PNG: '1:24.48' vs 1:200 was a false alarm)
    run = SC.check_scale_bar(str(tmp_path / "sheet.png"), 0, "sheet.png")
    assert run.status == "skipped" and run.findings == []


def test_floor_set_helpers():
    assert SC._set_key("SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf") == "SEN-DFE-XX"
    assert SC._set_key("SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf") == "SEN-DFE-XX"
    assert SC._set_key("secondary_type_3_600_ground_floor_001.pdf") == SC._set_key("secondary_type_3_600_first_floor_001.pdf")
    assert SC._vc_kind("Lift") == "lift" and SC._vc_kind("Stair (Ground)") == "stair" and SC._vc_kind("Riser") == "shaft"
    assert SC._vc_kind("Lift Landing") is None and SC._vc_kind("Stair Store") is None
    assert SC._vc_kind("VOID OVER SPORTS HALL") == "void"
    assert SC._DERIVED.search("dfe-school.vec6.pdf") and not SC._DERIVED.search("dfe-school.pdf")


DFE = Path(".stoa/datasets/stoa-like/dfe")
SEN = ["SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf",
       "SEN-DFE-XX-01-D-X-0202-Special_School_Plan_Level_01-A-C01.pdf"]


@pytest.mark.skipif(not (DFE / SEN[1]).exists(), reason="shared stoa-like copy not on this machine")
def test_stoa_floors_line_up_and_a_moved_lift_is_found(tmp_path):
    """The main-line DfE SEN set: Level 00 + Level 01 uploaded together line up (stairs + lift, voids over the hall);
    a copy of Level 01 with its lift drawn 1 m east (experiments/f3/seed_move.py) is reported on both sheets."""
    import shutil

    up = tmp_path / "abcdef012345"
    up.mkdir()
    for n in SEN:
        shutil.copy(DFE / n, up / n)
    rep = SC.check(str(up / SEN[0]), 0, f"upload/abcdef012345/{SEN[0]}", checks=["floors_align"])
    assert rep["findings"] == []
    txt = " ".join(p["what"] for p in rep["passed"])
    assert "Lift (Level 00) and Lift (Level 01) line up" in txt and "Multi-Purpose Hall" in txt
    # the visitor leaves Level 01 out of the project (T3's project file): no floor to compare, and the cache key moves
    import json

    sig0 = SC._set_sig(up / SEN[0], f"upload/abcdef012345/{SEN[0]}")
    (up / ".stoa_project.json").write_text(json.dumps({"sheets": [
        {"id": f"upload/abcdef012345/{SEN[1]}#0", "user": {"include": False}}]}))
    assert SC._set_sig(up / SEN[0], f"upload/abcdef012345/{SEN[0]}") != sig0
    rep = SC.check(str(up / SEN[0]), 0, f"upload/abcdef012345/{SEN[0]}", checks=["floors_align"])
    assert rep["checks"][0]["status"] == "skipped" and not rep["findings"]
    # a NEW folder (E1's linework caches pages by path: an upload never changes its bytes under the same path)
    up2 = tmp_path / "abcdef012346"
    up2.mkdir()
    shutil.copy(DFE / SEN[0], up2 / SEN[0])
    subprocess.run([sys.executable, _release_workspace_script(REPO / "experiments/f3/seed_move.py"), str(DFE / SEN[1]), str(up2 / SEN[1]), "0",
                    "330,832,373,877", "1.0", "0", "200"], check=True, capture_output=True)
    for n in SEN:
        rep = SC.check(str(up2 / n), 0, f"upload/abcdef012346/{n}", checks=["floors_align"])
        assert [f["check"] for f in rep["findings"]] == ["floors_align"], rep["findings"]
        f = rep["findings"][0]
        assert "Lift" in f["title"] and {e["file"] for e in f["evidence"]} == {f"upload/abcdef012346/{m}" for m in SEN}


def test_picture_scale_bar_vs_printed_scale_on_sheet_size(tmp_path, monkeypatch):
    """On a PICTURE: bar px/m vs printed 1:N on the printed ISO size over the image's long side (stubbed reading of a
    1414 x 1000 px 'A2' sheet: 1:200 -> 1414 / 594 mm x 5 mm/m = 11.9 px/m)."""
    from types import SimpleNamespace

    from PIL import Image

    import importlib

    V = importlib.import_module("stoa.sheet.views")  # stoa.sheet exports a function named views

    img = tmp_path / "sheet.png"
    Image.new("L", (1414, 1000), 255).save(img)

    def fake(printed: str, bar_ratio: float, size: str = "A2"):
        lines = [SimpleNamespace(text=printed, box=(10, 10, 60, 20)), SimpleNamespace(text=size, box=(80, 10, 95, 20))]
        monkeypatch.setattr(V, "page_reading", lambda *a, **k: (lines, None, None, None, None, lambda b: list(b)))
        monkeypatch.setattr(V, "scale_bars", lambda ls: [{"ratio": bar_ratio, "box": (100, 900, 300, 920),
                                                          "labels": ["0", "10m"]}])

    k = 1000.0 / (1414 / 594 * 5) / (25.4 / 72)  # the bar 'ratio' (px read as pt) that means 1:200 on this A2 picture
    fake("SCALE 1:200", k)
    run = SC._bar_vs_paper(str(img), "x.png", object(), SC._Run("scale_bar"), [])
    assert run.compared == 1 and not run.findings and "✓" in run.passed[0]["what"]
    fake("SCALE 1:100", k)  # the title block says 1:100 but the bar (and the drawing) is 1:200
    run = SC._bar_vs_paper(str(img), "x.png", object(), SC._Run("scale_bar"), [])
    assert len(run.findings) == 1 and run.findings[0]["delta"]["pct"] < -40
    fake("SCALE 1:200", k, size="A1")  # several sizes / grid letters 'A1', 'A2' -> skipped, never a finding
    monkeypatch.setattr(V, "page_reading", lambda *a, **k: ([SimpleNamespace(text="1:200", box=(0, 0, 1, 1)),
                                                             SimpleNamespace(text="A1", box=(0, 0, 1, 1)),
                                                             SimpleNamespace(text="A2", box=(0, 0, 1, 1))],
                                                            None, None, None, None, lambda b: list(b)))
    run = SC._bar_vs_paper(str(img), "x.png", object(), SC._Run("scale_bar"), [])
    assert run.status == "skipped" and not run.findings


def test_picture_report_cache_is_keyed_on_its_text_layer(tmp_path, monkeypatch):
    """The upload warm-up reads a scan's text AFTER the first background check: the cached report must not outlive it."""
    from PIL import Image

    img = tmp_path / "scan.png"
    Image.new("L", (141, 100), 255).save(img)
    monkeypatch.setattr(SC, "_image_layer", lambda p: None)
    assert SC._layer_sig(img) == (-1, 0.0)
    monkeypatch.setattr(SC, "_image_layer", lambda p: [("1:200", (0, 0, 1, 1), 0.9)] * 3)
    assert SC._layer_sig(img) == (3, 0.0)
    (tmp_path / "scan.vec6.pdf").write_bytes(b"%PDF-1.4\n")  # the first measured question traced it: a new key
    assert SC._layer_sig(img)[1] > 0
    assert SC._layer_sig(Path("x.pdf")) == 0


def _ifc_plan(name: str) -> str:
    from stoa.data import data_root

    p = data_root() / "data" / "ifc_models" / name
    if not p.exists() or not p.with_suffix(".svg").exists():
        pytest.skip("ifc_models plan not on this machine")
    return str(p)


def test_plan_vs_model_agrees_on_a_clean_render_and_finds_an_erased_door(tmp_path):
    plan = _ifc_plan("bimspeed-warmond_ar_plan-03-1-level.pdf")
    notes: list = []
    run = SC.check_plan_vs_model(plan, 0, plan, notes=notes)
    assert run.status == "ran" and not run.findings and len(run.passed) >= 3, (run.why, notes)
    assert any("42 drawn = 42" in p["what"] for p in run.passed)
    # one door erased from the drawing (SVG tag + PDF linework), still in the model -> one finding, boxed on its spot
    out = tmp_path / "seed"
    rel = "data/ifc_models/bimspeed-warmond_ar_plan-03-1-level.pdf"
    subprocess.run([sys.executable, _release_workspace_script(REPO / "experiments/f3/seed_pvm.py"), rel, "IfcDoor", "7", str(out)],
                   check=True, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO)}, capture_output=True)
    seeded = str(out / Path(rel).name)
    run = SC.check_plan_vs_model(seeded, 0, seeded, notes=[])
    assert len(run.findings) == 1
    f = run.findings[0]
    assert f["readings"][0]["value"] == 42 and f["readings"][1]["value"] == 41
    ev = f["evidence"][0]
    assert ev["ifc_guid"] == "0qGwjGR$P5ifLdP20siN1Q"
    assert ev["bbox"][0] < 225 < ev["bbox"][2] and ev["bbox"][1] < 85  # the erased door sat at x 218-231, y 80-94


def test_plan_vs_model_skips_sheets_without_a_model(tmp_path):
    p = _table_pdf(tmp_path, [("A", "1")])
    run = SC.check_plan_vs_model(p, 0, p)
    assert run.status == "skipped"


def test_pictures_never_start_a_text_read(tmp_path, monkeypatch):
    """The uploads sweep found check_area_table -> areatable.image_rows -> _cached_layer -> text_layer() starting a cold
    OCR read (123 s on a USACE scan). Every check on a picture must use cached text only."""
    pil = pytest.importorskip("PIL.Image")
    from stoa.solvers.raster import textlayer as TL

    calls = []

    def trap(*a, **k):
        calls.append(a)
        raise RuntimeError("no text reads in a self-check")

    monkeypatch.setattr(TL, "text_layer", trap)
    from stoa.evidence import areatable as A

    # what areatable._cached_layer does when a VLM layer is on disk: text_layer() (which may start a cold OCR read)
    monkeypatch.setattr(A, "_cached_layer", lambda path: TL.text_layer(path))
    img = tmp_path / "sheet.png"
    pil.new("RGB", (800, 600), "white").save(img)
    rep = SC.check(str(img), 0)
    assert calls == [] and rep["findings"] == []


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
def test_selfcheck_question_matcher_node_tests():
    """Which typed questions the self-check answers (web/src/features/selfcheck/question.ts)."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    test = root / "web" / "src" / "features" / "selfcheck" / "question.test.ts"
    r = subprocess.run([_node(), "--test", str(test)], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ℹ fail 0" in r.stdout


def test_a_number_with_a_unit_word_is_not_a_dimension():
    """Sports Complex A-801: 'CLINIC 2470 SF' in the Finish Room Schedule was read as a 2470 mm dimension (false alarm).
    Words are (x0, y0, x1, y1, text); the page is /Rotate 90, so the unit can sit below the number in PDF space."""
    num = (100.0, 200.0, 120.0, 208.0)
    assert SC._unit_next(num, [(122.0, 200.0, 132.0, 208.0, "SF")])           # '2470 SF' left to right
    assert SC._unit_next((100.0, 200.0, 108.0, 220.0), [(100.0, 222.0, 108.0, 230.0, "SF")])  # rotated text
    assert SC._unit_next(num, [(122.0, 200.0, 130.0, 208.0, "m²")])
    assert not SC._unit_next(num, [(122.0, 200.0, 140.0, 208.0, "TYP")])      # a note word: still a dimension
    assert not SC._unit_next(num, [(160.0, 200.0, 170.0, 208.0, "SF")])       # far away: another number's unit


def test_a_scanned_pdf_is_checked_as_its_picture(tmp_path, monkeypatch):
    """A scanned print (page 0 = one picture, no text, no lines) gave 'nothing to check' as a PDF: it is now checked
    like a picture (normalize.scan_png, the same render the upload readers read), never starting a text read, and
    evidence boxes come back in the PDF's points."""
    pil = pytest.importorskip("PIL.Image")
    pymupdf = pytest.importorskip("pymupdf")
    from stoa.solvers.raster import textlayer as TL

    monkeypatch.setattr(TL, "text_layer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no text reads")))
    img = tmp_path / "scan.png"
    pil.new("RGB", (1200, 800), "white").save(img)
    pdf = tmp_path / "scanned.pdf"
    d = pymupdf.open()
    pg = d.new_page(width=600, height=400)
    pg.insert_image(pg.rect, filename=str(img))
    d.save(pdf)
    rep = SC.check(str(pdf), 0, "upload/abc/scanned.pdf")
    assert any("Scanned PDF" in n for n in rep["notes"]) and rep["findings"] == []
    drawn = tmp_path / "drawn.pdf"
    d2 = pymupdf.open()
    d2.new_page(width=600, height=400).insert_text((50, 50), "ROOM 101 OFFICE 12.5 m2 and more text here")
    d2.save(drawn)
    assert not any("Scanned PDF" in n for n in SC.check(str(drawn), 0)["notes"])
    # a picture box (image px at 2 px per pt) -> PDF points on page 0 of the uploaded file
    f = {"evidence": [{"file": "x.png", "page": None, "bbox": [100, 200, 300, 400], "bbox_space": "image_px"}],
         "zoom": [88, 188, 312, 412]}
    SC._px_to_pt([f], [], "upload/abc/scanned.pdf", 2.0)
    e = f["evidence"][0]
    assert (e["file"], e["page"], e["bbox_space"], e["bbox"]) == ("upload/abc/scanned.pdf", 0, "pdf_pt", [50, 100, 150, 200])
    assert f["zoom"] == [44, 94, 156, 206]


def test_a_scanned_set_is_checked_page_by_page(tmp_path, monkeypatch):
    """Page 2 of a scanned set: the same one-page copy + render the upload readers make (so their OCR is found), the
    report keeps the PDF page, a drawn page of the same set is not treated as a scan."""
    pil = pytest.importorskip("PIL.Image")
    pymupdf = pytest.importorskip("pymupdf")
    from stoa.solvers.raster import textlayer as TL

    monkeypatch.setattr(TL, "text_layer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no text reads")))
    img = tmp_path / "scan.png"
    pil.new("RGB", (1200, 800), "white").save(img)
    pdf = tmp_path / "set.pdf"
    d = pymupdf.open()
    d.new_page(width=600, height=400).insert_text((50, 50), "COVER SHEET drawing list and general notes here")
    for _ in range(2):
        d.new_page(width=600, height=400).insert_image(pymupdf.Rect(0, 0, 600, 400), filename=str(img))
    d.save(pdf)
    rep = SC.check(str(pdf), 2, "upload/abc/set.pdf")
    assert rep["page"] == 2 and any("Scanned PDF" in n for n in rep["notes"])
    assert (tmp_path / "set.p3.scan.png").exists()  # = normalize.prepare's copy for page 3
    assert SC._scan_of(str(pdf), 0) is None and not SC._page_is_scan(str(pdf), 0) and SC._page_is_scan(str(pdf), 1)


def test_picture_dimensions_come_from_its_traced_copy_and_only_agree(tmp_path, monkeypatch):
    """A picture's traced copy (cache-only) gives the dimension checks a second reading; an agreement is kept, a
    disagreement is never reported (an OCR misread looks exactly like a drawing slip) and the run is skipped with why."""
    pil = pytest.importorskip("PIL.Image")
    img = tmp_path / "plan.png"
    pil.new("RGB", (400, 300), "white").save(img)
    assert SC._traced_copy(str(img)) is None
    c, d = SC._dims_on_picture(str(img), "upload/x/plan.png")
    assert c.status == d.status == "skipped" and "traced copy" in d.why
    (tmp_path / "plan.s6000.vec6.pdf").write_bytes(b"%PDF-1.4\n")
    assert SC._traced_copy(str(img)).endswith("plan.s6000.vec6.pdf")

    def fake(path, page, file):
        a, b = SC._Run("dimension_chains"), SC._Run("dimension_drawn")
        a.compared, b.compared = 1, 3
        a.findings = [{"title": "Dimension chain 4975 + 4975 ≠ 9590"}]
        b.passed = [{"check": "dimension_drawn", "what": "3 dimension numbers match their drawn length ✓"}]
        return a, b

    monkeypatch.setattr(SC, "check_dimensions", fake)
    c, d = SC._dims_on_picture(str(img), "upload/x/plan.png")
    assert c.findings == [] and c.status == "skipped" and "not reported" in c.why
    assert d.status == "ran" and "traced lines" in d.passed[0]["what"]


def test_a_phone_photo_is_read_through_its_straightened_copy(tmp_path):
    """O1's photo step OCRs the deskewed copy, sometimes never the raw photo: the self-check finds that copy from its
    sidecar (cache-only, only when the sidecar is valid for THIS file) and maps evidence back through the homography."""
    import json

    pil = pytest.importorskip("PIL.Image")
    from stoa.solvers.raster import photo as PH

    raw = tmp_path / "plan.jpg"
    pil.new("RGB", (400, 300), "white").save(raw)
    flat = tmp_path / "plan.photo.png"
    pil.new("RGB", (200, 150), "white").save(flat)
    H = [[2.0, 0.0, 10.0], [0.0, 2.0, 20.0], [0.0, 0.0, 1.0]]  # deskewed px -> photo px
    key = {"v": PH.VERSION, "flat": PH.FLATTEN, "mtime": raw.stat().st_mtime, "size": raw.stat().st_size}
    side = tmp_path / f"plan.photo{'' if PH.FLATTEN else '-nf'}.json"
    side.write_text(json.dumps({"key": key, "d": {"path": str(flat), "to_photo": H}}))
    assert SC._photo_of(str(raw)) == (str(flat), H)
    side.write_text(json.dumps({"key": {**key, "v": "old"}, "d": {"path": str(flat), "to_photo": H}}))
    assert SC._photo_of(str(raw)) is None  # a sidecar of an older photo step: not trusted
    f = {"evidence": [{"bbox": [10, 10, 20, 30], "bbox_space": "image_px"}], "zoom": [0, 0, 50, 50]}
    SC._to_photo([f], [], H)
    assert f["evidence"][0]["bbox"] == [30.0, 40.0, 50.0, 80.0] and f["zoom"] == [10.0, 20.0, 110.0, 120.0]


UK14 = Path(".stoa/datasets/stoa-like/dfe-baseline-2014")
JMI210 = UK14 / "baseline-design-210-place-primary-school__1fe_210_place_jmi_primary_school.pdf"
SEC600 = UK14 / "baseline-designs-600-place-secondary-sch__secondary_type_3_600_ground_floor_001.pdf"


def _seed(src: str, old: str, new: str, out: Path) -> str:
    r = subprocess.run([sys.executable, _release_workspace_script(REPO / "experiments/f3/seed.py"), src, "0", old, new, str(out)],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO)})
    assert r.returncode == 0, r.stderr
    return str(out)


def test_uk_area_tag_reader_reads_name_over_area():
    assert SC._UK_AREA.match("A: 62 m2") and SC._UK_AREA.match("6 m2") and SC._UK_AREA.match("(13.0 m2)")
    assert not SC._UK_AREA.match("GIFA 1645") and not SC._UK_AREA.match("Yr 3")


def test_stoa_uk_room_areas_agree_and_a_retyped_area_is_only_noted(tmp_path):
    src = _need(JMI210)  # DfE 1FE primary: 'Yr 3' over 'A: 62 m2', scale only in the title block ('1:200@A3')
    rep = SC.check(src, 0, checks=["room_areas"])
    run = next(c for c in rep["checks"] if c["check"] == "room_areas")
    assert run["status"] == "ran" and rep["findings"] == []
    line = next(p["what"] for p in rep["passed"] if p["check"] == "room_areas")
    n = int(line.split()[0])
    assert n >= 15 and "1:200" in line
    seeded = SC.check(_seed(src, "62", "92", tmp_path / "jmi_seeded.pdf"), 0, checks=["room_areas"])
    assert seeded["findings"] == []  # agreements only: a disagreement on these plans is noted, never reported
    line2 = next(p["what"] for p in seeded["passed"] if p["check"] == "room_areas")
    assert int(line2.split()[0]) == n - 1
    assert any("92 m2" in x for x in seeded["notes"])


def test_stoa_uk_comma_dimensions_chain_and_a_seeded_slip_is_found(tmp_path):
    src = _need(SEC600)  # '69,600 + 28,030 = 97,630' printed with thousands commas
    rep = SC.check(src, 0, checks=["dimension_chains", "dimension_drawn"])
    assert rep["findings"] == []
    assert any("69,600 + 28,030 = 97,630" in p["what"] for p in rep["passed"])
    seeded = SC.check(_seed(src, "28,030", "28,330", tmp_path / "sec_seeded.pdf"), 0,
                      checks=["dimension_chains", "dimension_drawn"])
    assert [f["check"] for f in seeded["findings"]] == ["dimension_chains"]
    assert "28,330" in seeded["findings"][0]["title"]


def test_picture_room_areas_come_from_its_traced_copy_and_only_agree(tmp_path, monkeypatch):
    """Room areas on a picture: read on its traced copy, agreements condensed to one line, every disagreement only
    noted (on 9 traced scans every 'disagreement' was a tracer / OCR artefact)."""
    pil = pytest.importorskip("PIL.Image")
    img = tmp_path / "plan.png"
    pil.new("RGB", (400, 300), "white").save(img)
    r = SC._room_areas_on_picture(str(img), "upload/x/plan.png")
    assert r.status == "skipped" and "traced copy" in r.why
    (tmp_path / "plan.vec6.pdf").write_bytes(b"%PDF-1.4\n")

    def fake(path, page, file, notes=None):
        run = SC._Run("room_areas")
        run.compared = 3
        run.findings = [{"title": "Korridor 2.90: printed 118.26 m², drawn ≈ 106.10 m²"}]
        run.passed = [{"check": "room_areas", "what": f"Zimmer 2.0{i}: printed 15.40 m², traced 14.98 m² (-2.7 %) ✓"}
                      for i in (1, 2)]
        return run

    monkeypatch.setattr(SC, "check_room_areas", fake)
    notes: list = []
    r = SC._room_areas_on_picture(str(img), "upload/x/plan.png", notes)
    assert r.findings == [] and r.status == "ran" and len(r.passed) == 1
    assert r.passed[0]["what"].startswith("2 printed room areas match") and "Zimmer 2.01 15.40 m² ≈ 14.98" in \
        r.passed[0]["what"]
    assert notes and "not reported" in notes[0]


def test_stoa_uk_secondary_metric_room_tags_are_agreement_only():
    """UK secondary plan with titled views at a scale: the per-room path reads its '6 m2' tags. It reported 12
    'differences' here (open hall, changing rooms, unlabelled tags, 4 m2 stores at the rounding edge), all tracer /
    tag-reader artefacts: m² tags are compared for agreement only, many agreeing rooms are one line."""
    rep = SC.check(_need(SEC600), 0, checks=["room_areas"])
    assert rep["findings"] == []
    lines = [p["what"] for p in rep["passed"] if p["check"] == "room_areas"]
    assert len(lines) == 1 and int(lines[0].split()[0]) >= 15
    assert any("not reported" in n for n in rep["notes"])


def test_picture_room_tag_disagreements_are_only_noted(tmp_path, monkeypatch):
    """Room tags on a picture are OCR text: one tag read twice and spelt two ways ('Operations Maniager Office' /
    'Operations Manager Office', X1's scanned AFCEE A-101) looked like 'Room number 2.06 is used twice'."""
    pil = pytest.importorskip("PIL.Image")
    img = tmp_path / "scan.png"
    pil.new("RGB", (400, 300), "white").save(img)
    monkeypatch.setattr(SC, "_image_layer", lambda p: object())

    def fake(path, page, file, layer=None):
        run = SC._Run("room_tags")
        run.compared = 22
        run.findings = [{"title": "Room number 2.06 is used twice", "severity": "error", "sure": 0.8}]
        return run

    monkeypatch.setattr(SC, "check_room_tags", fake)
    rep = SC.check(str(img), 0, checks=["room_tags"])
    assert rep["findings"] == []
    assert rep["checks"][0]["status"] == "skipped" and "OCR" in rep["checks"][0]["why"]
    assert any("2.06" in n and "not reported" in n for n in rep["notes"])


def test_a_big_picture_is_read_through_its_6000px_copy_cache_only(tmp_path):
    """Pictures over 6000 px are read on '<stem>.s6000.png' (normalize.shrunk_copy): the self-check uses that copy's
    text layer when its sidecar is valid, and never makes one itself."""
    import json

    pil = pytest.importorskip("PIL.Image")
    from stoa.formats import normalize as N

    img = tmp_path / "scan.png"
    pil.new("RGB", (120, 80), "white").save(img)
    assert SC._shrunk_of(str(img)) is None  # no copy: nothing is made
    assert not list(tmp_path.glob("scan.s*.png"))
    cp = tmp_path / f"scan.s{N.IMAGE_MAX_SIDE}.png"
    pil.new("RGB", (60, 40), "white").save(cp)
    meta = cp.with_name(cp.name + ".json")
    meta.write_text(json.dumps({"v": N.SHRINK_VERSION, "src_size": [120, 80], "factor": 0.5, "note": "x"}))
    assert SC._shrunk_of(str(img)) == (str(cp), 0.5)
    meta.write_text(json.dumps({"v": N.SHRINK_VERSION, "src_size": [121, 80], "factor": 0.5, "note": "x"}))
    assert SC._shrunk_of(str(img)) is None  # a copy of another picture
    f = [{"evidence": [{"bbox_space": "image_px", "bbox": [10, 10, 20, 20]}], "zoom": [0, 0, 30, 30]}]
    SC._unshrink(f, [], 0.5)
    assert f[0]["evidence"][0]["bbox"] == [20, 20, 40, 40] and f[0]["zoom"] == [0, 0, 60, 60]


# ------------------------------------------------------------------------- a print vs its original: revision (UX C-10)
def _w(t, x0, y0, x1, y1):
    return (t, float(x0), float(y0), float(x1), float(y1))


# a DfE-like title block: the history table (row 'C01 2026-01-12 …' over its 'REVISION DATE DESCRIPTION' header) and the
# current-revision box ('REVISION' label, 'C01' under it, next to the drawing number)
_BLOCK = [_w("C01", 1347, 995, 1360, 1002), _w("2026-01-12", 1374, 995, 1410, 1002), _w("Issued", 1421, 995, 1440, 1002),
          _w("REVISION", 1336, 1009, 1369, 1017), _w("DATE", 1375, 1009, 1394, 1017), _w("DESCRIPTION", 1421, 1009, 1469, 1017),
          _w("NUMBER", 1574, 1123, 1604, 1131), _w("SEN-DFE-XX-00-D-X-0201", 1337, 1141, 1481, 1154),
          _w("REVISION", 1610, 1123, 1643, 1131), _w("C01", 1616, 1138, 1638, 1152)]


def test_revision_box_and_history_are_read_from_a_title_block():
    r = SC._revision_from_words(_BLOCK)
    assert r["rev"] == "C01" and r["box"] == [1616, 1138, 1638, 1152]
    assert [h["rev"] for h in r["history"]] == ["C01"] and r["history"][0]["date"] == "2026-01-12"
    # an OCR line 'REVISION DATE' is a history header (split into words), not the current-revision box
    ocr = [_w("C01", 3765, 2770, 3803, 2789), _w("2026-01-12", 3839, 2770, 3943, 2789), _w("REVISION DATE", 3735, 2811, 3898, 2832),
           _w("REVISION", 4498, 3122, 4594, 3142), _w("CO1", 4515, 3167, 4575, 3195)]
    r = SC._revision_from_words(ocr)
    assert r["rev"] == "C01" and [h["rev"] for h in r["history"]] == ["C01"]
    assert SC._rev_norm("c0l") == "C01" and SC._rev_norm("P02.") == "P02" and SC._rev_norm("ROOF") == "ROOF"
    assert SC._revision_from_words([_w("NUMBER", 0, 0, 30, 8), _w("A-101", 0, 12, 30, 20)]) is None


def test_a_print_is_out_of_date_only_when_a_revision_table_says_so(monkeypatch):
    revs = {}
    monkeypatch.setattr(SC, "sheet_revision", lambda p, page=0: revs.get(p))

    def rv(rev, hist=()):
        return {"rev": rev, "box": [0, 0, 10, 10], "space": "pdf_pt", "read": "pdf text",
                "history": [{"rev": h, "date": "2026-01-12", "box": [0, 20, 10, 30]} for h in hist]}

    revs.update(print=rv("C01", ["C01"]), orig=rv("C01", ["C01"]))
    r = SC.compare_revision("print", 0, "orig", 0)
    assert r["status"] == "same" and r["severity"] is None and len(r["evidence"]) == 2
    revs["orig"] = rv("C02", ["C01"])  # the original's table lists the print's C01 as an earlier issue
    r = SC.compare_revision("print", 0, "orig", 0)
    assert r["status"] == "older" and r["severity"] == "error" and "C01" in r["line"] and "C02" in r["line"]
    r = SC.compare_revision("orig", 0, "print", 0)  # the other way round: the print is the newer one
    assert r["status"] == "newer" and r["severity"] == "error"
    revs["orig"] = rv("C07", [])  # 'C01' vs 'C07' with no table saying which came first: an OCR misread looks the same
    r = SC.compare_revision("print", 0, "orig", 0)
    assert r["status"] == "differs" and r["severity"] == "check"
    revs["orig"] = None
    r = SC.compare_revision("print", 0, "orig", 0)
    assert r["status"] == "unread" and r["severity"] is None and not r["evidence"]


@pytest.mark.skipif(not (DFE / SEN[0]).exists(), reason="shared stoa-like copy not on this machine")
def test_the_dfe_sheet_reads_revision_c01():
    r = SC.sheet_revision(str(DFE / SEN[0]), 0)
    assert r and r["rev"] == "C01" and r["space"] == "pdf_pt" and [h["rev"] for h in r["history"]] == ["C01"]


def _release_workspace_script(p):
    """Added by the release export: scripts under experiments/ stay in the team's hackathon workspace repository."""
    import pathlib

    import pytest

    p = pathlib.Path(p)
    if not p.exists():
        pytest.skip(f"needs {p.name}, a script in the workspace repository's experiments/ folder (not in this repository)")
    return str(p)
