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
    assert [r.path for r in SC.router.routes] == ["/feat/selfcheck/run", "/feat/selfcheck/checks"]
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
    r = subprocess.run([sys.executable, str(REPO / "experiments/f3/seed.py"), src, "0", old, new, str(out)],
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
    r = subprocess.run([sys.executable, str(REPO / "experiments/f3/seed.py"), src, "68", "104B", "104C", str(out)],
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
    subprocess.run([sys.executable, str(REPO / "experiments/f3/seed_move.py"), str(DFE / SEN[1]), str(up2 / SEN[1]), "0",
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
    assert SC._layer_sig(img) == -1
    monkeypatch.setattr(SC, "_image_layer", lambda p: [("1:200", (0, 0, 1, 1), 0.9)] * 3)
    assert SC._layer_sig(img) == 3
    assert SC._layer_sig(Path("x.pdf")) == 0
