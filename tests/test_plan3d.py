"""stoa.geometry.plan3d: the 3D-from-the-plan geometry the web app extrudes (W5)."""

import pytest

from stoa.data import abspath
from stoa.geometry.plan3d import plan3d, room_kind

P = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf"
needs_data = pytest.mark.skipif(not abspath(P).exists(), reason="dataset not available")


def test_room_kind():
    assert room_kind("Zimmer") == "bedroom"
    assert room_kind("Küche") == "kitchen"
    assert room_kind("Wohnen/Essen") == "living"
    assert room_kind("Balkon") == "outdoor"
    assert room_kind("Korridor") == "corridor"
    assert room_kind(None) == "other"


@needs_data
def test_demo_floor_geometry():
    d = plan3d(P)
    assert d["ok"]
    assert len(d["walls"]) > 10 and len(d["rooms"]) > 20
    assert all(len(w) >= 3 for w in d["walls"])
    assert all(x["leaf"] for x in d["doors"])  # every door has its drawn leaf
    assert all(len(w["foot"]) >= 3 for w in d["windows"])
    labels = {u["label"] for u in d["units"]}
    assert {"A", "B"} <= labels
    # the DXF -> PDF affine maps room 2.10's DXF box onto the page
    tf = d["pdf"]
    r = next(r for r in d["rooms"] if r["number"] == "2.10")
    x = tf["s"] * r["anchor"][0] + tf["a"]
    y = -tf["s"] * r["anchor"][1] + tf["b"]
    assert 0 < x < tf["page_w"] and 0 < y < tf["page_h"]


def test_no_dxf_is_a_reason_not_an_error():
    d = plan3d("data/cubicasa5k/colorful-4539.png")
    assert d["ok"] is False and "DXF" in d["error"]


@needs_data
def test_door_openings_get_a_lintel_rectangle():
    from shapely.geometry import Polygon

    d = plan3d(P)
    gaps = [x for x in d["doors"] if x.get("gap")]
    assert len(gaps) >= 0.9 * len(d["doors"])  # 35 of 36 on the demo floor
    for x in gaps:
        g, leaf = Polygon(x["gap"]), Polygon(x["leaf"])
        assert g.is_valid and 0.03 < g.area < 1.5
        assert g.buffer(0.02).contains(leaf.centroid)  # the opening is where the panel is drawn


@needs_data
def test_fixtures_come_from_the_fixture_layer():
    # captain 16:00: show the fixtures the drawing contains (kitchen counters, basins, WCs) as low blocks, nothing invented
    d = plan3d(P)
    fx = d["fixtures"]
    assert len(fx) >= 20
    assert all(len(f["poly"]) >= 3 and 0.3 <= f["h"] <= 1.0 for f in fx)
    assert any(f["room_kind"] == "kitchen" and f["h"] == 0.9 for f in fx)  # counters at counter height
