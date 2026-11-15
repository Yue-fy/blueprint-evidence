"""O1 cycle 15: an area tag never admits a 2-label scale bar (scalebar._AREA_TAG, STOA_O1_BAR_OFF=areaunit)."""

from stoa.solvers.raster import scalebar as SB
from stoa.solvers.raster import textlayer as T


def _it(text, box):
    return T.TextItem(text, [float(v) for v in box])


def test_area_tag_does_not_admit_a_two_label_bar(monkeypatch):
    # F2 00:59, CubiCasa Finnish flat (no bar on the sheet): '0' and '60' in a row, the area tag '13.0 m²' beside them
    items = [_it("0", [10.6, 488.3, 22.2, 508.8]), _it("60", [637.2, 474.0, 654.1, 502.0]),
             _it("13.0 m²", [300, 470, 360, 500]), _it("MH", [300, 440, 330, 465])]
    assert SB._bars_from_items(items, items, "layer") == []
    monkeypatch.setattr(SB, "BAR_OFF", {"areaunit"})
    bs = SB._bars_from_items(items, items, "layer")  # the old behaviour: a 0-60 m 'bar', 10.5 px per m
    assert len(bs) == 1 and bs[0].unit == "m"


def test_area_tag_still_names_the_unit_system_of_a_full_bar():
    # EFA sheets: the real '0 1 2 … 10' bar prints no unit word; 'TARGET: 4831m²' beside it says metric
    items = [_it(str(v), [686 + 29.6 * v - 4, 3401, 686 + 29.6 * v + 4, 3418]) for v in range(11)]
    items.append(_it("TARGET: 4831m²", [700, 3430, 800, 3448]))
    bs = SB._bars_from_items(items, items, "layer")
    assert len(bs) == 1 and bs[0].unit == "m" and abs(bs[0].px_per_unit - 29.6) < 0.1
