"""O1 cycle 7: phone photos of printed sheets (stoa/solvers/raster/photo.py) and the photo rule in printed_bar."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from stoa.solvers.raster import photo as P


def _sheet(w=1414, h=1000):
    """A synthetic A-series sheet: white paper, a black rectangle room 400 x 300 px at (300, 250), a border."""
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(img, (20, 20), (w - 21, h - 21), (0, 0, 0), 2)
    cv2.rectangle(img, (300, 250), (700, 550), (0, 0, 0), 6)
    cv2.putText(img, "1:100 @ A1", (w - 300, h - 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 2)
    return img


def _photo(sheet, seed=3, tilt=0.03):
    """Perspective-warped sheet on a dark table with uneven light (like experiments/o1/fake_photo.py)."""
    rng = np.random.default_rng(seed)
    h, w = sheet.shape[:2]
    pad = int(0.08 * max(w, h))
    canvas = cv2.copyMakeBorder(sheet, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(90, 95, 100))
    H, W = canvas.shape[:2]
    src = np.float32([[pad, pad], [W - pad, pad], [W - pad, H - pad], [pad, H - pad]])
    j = lambda: rng.uniform(-tilt, tilt) * max(w, h)  # noqa: E731
    dst = np.float32([[pad + j(), pad + j()], [W - pad + j(), pad + j()], [W - pad + j(), H - pad + j()],
                      [pad + j(), H - pad + j()]])
    M = cv2.getPerspectiveTransform(src, dst)
    canvas = cv2.warpPerspective(canvas, M, (W, H), borderValue=(90, 95, 100))
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    light = 0.85 + 0.15 * (xx / W)
    canvas = np.clip(canvas.astype(np.float32) * light[..., None], 0, 255).astype(np.uint8)
    # where the sheet's corners went: sheet px -> photo px
    to_photo = M @ np.array([[1, 0, pad], [0, 1, pad], [0, 0, 1]], dtype=np.float64)
    return canvas, to_photo


def test_deskew_recovers_sheet(tmp_path):
    sheet = _sheet()
    ph, to_photo = _photo(sheet)
    src = tmp_path / "ph.jpg"
    cv2.imwrite(str(src), ph, [cv2.IMWRITE_JPEG_QUALITY, 92])
    d = P.deskew(src, out_dir=tmp_path)
    assert d is not None and d.snapped and d.paper == "ISO A"
    W, H = d.size
    assert abs(W / H - 2 ** 0.5) < 0.01
    # the room corners land where they should (deskewed px -> photo px -> sheet px)
    back = np.linalg.inv(to_photo)
    sx, sy = 1414 / W, 1000 / H
    for x, y in [(300, 250), (700, 550)]:
        px = P.map_points(d.to_photo, [[x / sx, y / sy]])[0]
        q = back @ np.array([px[0], px[1], 1.0])
        assert abs(q[0] / q[2] - x) < 4 and abs(q[1] / q[2] - y) < 4
    out = cv2.imread(d.path, cv2.IMREAD_GRAYSCALE)
    assert (out > 245).mean() > 0.9  # the light is evened out: paper is white again
    assert P.photo_family(d.path) == "ISO A"


def test_not_a_photo(tmp_path):
    clean = _sheet()
    p = tmp_path / "clean.png"
    cv2.imwrite(str(p), np.pad(clean, ((0, 0), (0, 0), (0, 0)), constant_values=255))
    assert P.deskew(p, out_dir=tmp_path) is None  # paper touches the frame
    # a crop of a plan on white
    crop = np.full((900, 1600, 3), 255, np.uint8)
    cv2.rectangle(crop, (100, 100), (1500, 800), (0, 0, 0), 8)
    p2 = tmp_path / "crop.png"
    cv2.imwrite(str(p2), crop)
    assert P.deskew(p2, out_dir=tmp_path) is None
    assert P.photo_family(str(p2)) is None


def test_cached_and_prepare_restore(tmp_path):
    from stoa.contract import Evidence

    sheet = _sheet()
    ph, _ = _photo(sheet, seed=5)
    src = tmp_path / "ph2.jpg"
    cv2.imwrite(str(src), ph)
    d1 = P.cached_deskew(src, tmp_path)
    d2 = P.cached_deskew(src, tmp_path)
    assert d1 is not None and d2 is not None and d1.path == d2.path and d1.to_photo == d2.to_photo

    class Prep:
        notes: list = []

    prep = Prep()
    prep.notes = []
    assert P.prepare_hook(prep, src, tmp_path)
    assert prep.kind == "photo" and prep.path.endswith(".photo.png") and prep.notes
    ev = Evidence(file=prep.path, page=None, bbox=[10, 10, 20, 20], note="x", bbox_space="image_px")
    assert P.restore_ev(ev, prep, str(src))
    assert ev.file == str(src) and ev.bbox[2] > ev.bbox[0]


def test_aspect_physical_camera():
    W, H, f = 4000, 3000, 2400.0
    K = np.array([[f, 0, W / 2], [0, f, H / 2], [0, 0, 1]])
    R = cv2.Rodrigues(np.radians(np.array([18.0, -9.0, 4.0])))[0]
    pts = np.array([[-0.707, -0.5, 0], [0.707, -0.5, 0], [0.707, 0.5, 0], [-0.707, 0.5, 0]])
    X = (R @ pts.T).T + np.array([0, 0, 2.2])
    x = (K @ X.T).T
    q = P._order(x[:, :2] / x[:, 2:])
    assert abs(P._aspect(q, W, H) - 1.414) < 0.005


def test_printed_bar_photo_named_paper(tmp_path):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    from stoa.solvers.raster import scalebar as SB
    from stoa.solvers.raster import textlayer as T

    info = PngInfo()
    info.add_text(P.PHOTO_KEY, "ISO A")
    p = tmp_path / "x.photo.png"
    info.add_text(P.PHOTO_DPI_KEY, "217.7")
    Image.new("RGB", (3600, 2546), "white").save(p, pnginfo=info)  # A3 prior, no DPI tag
    item = lambda t, y: T.TextItem(text=t, box=[100, y, 400, y + 20])  # noqa: E731
    tl = T.TextLayer(path=str(p), size=(3600, 2546), items=[item("Scale: 1:200 @ A1", 100), item("Plan", 200)])
    b = SB.printed_bar(str(p), tl)
    assert b is not None and b.paper == "A1" and b.dpi_from == "photo"
    # 1:200 on A1 = 1:400 on A3: px/m the same either way
    assert b.px_per_unit == pytest.approx(3600 / 841 * 1000 / 200, rel=1e-3)
    tl2 = T.TextLayer(path=str(p), size=(3600, 2546), items=[item("Scale 1:100", 100)])
    assert SB.printed_bar(str(p), tl2) is None
    g = SB.printed_bar(str(p), tl2, photo_guess=True)
    assert g is not None and g.dpi_from == "photo-guess" and "ASSUMING" in SB.scale_source(g)


def _tl(items, size=(1600, 700)):
    from stoa.solvers.raster import textlayer as T

    return T.TextLayer(path="x.png", size=size, items=[T.TextItem(text=t, box=list(b)) for t, b in items])


def test_loose_chain_bar_grid_bays():
    """X1 sl-134..137: a screenshot crop with only grid-bay strings: px/m from their spacing (true 24.6)."""
    from stoa.solvers.raster import chains as CH

    xs = [137, 313, 491, 666, 844, 1021, 1170, 1317, 1494]
    vals = ["7,200"] * 6 + ["4.800", "7.200", "7.200"]
    items = [(v, (x - 11, 91, x + 11, 99)) for v, x in zip(vals, xs)] + [("English 3", (814, 232, 875, 244))]
    b = CH.loose_chain_bar(_tl(items))
    assert b is not None and abs(b.px_per_unit - 24.6) < 0.3
    # one misread string ('1,200' for '7,200') ends the run; the longest fitting run still gives the scale
    items2 = [(("1,200" if k == 7 else v), bx) for k, (v, bx) in enumerate(items)]
    b2 = CH.loose_chain_bar(_tl(items2))
    assert b2 is not None and abs(b2.px_per_unit - 24.6) < 0.3
    # identical values (a height printed in each room of a row) are not a chain; nor are signed levels
    same = [("2,50", (x - 11, 91, x + 11, 99)) for x in (100, 300, 500, 700)]
    assert CH.loose_chain_bar(_tl(same)) is None
    lev = [(t, (x - 11, 91, x + 11, 99)) for t, x in zip(["62", "-120", "-52", "110"], (100, 160, 250, 380))]
    assert CH.loose_chain_bar(_tl(lev)) is None


def test_numbered_room_label_and_range():
    from stoa.solvers.raster import measure as M
    from stoa.solvers.raster import textqa as TQ

    tl = _tl([("English 1", (462, 232, 521, 244)), ("English 3", (814, 232, 875, 244)),
              ("English 11", (900, 232, 960, 244)), ("Learning Resource Centre", (1351, 227, 1521, 243))])
    assert M.find_label(tl, "What is the area of English 3?", loose=True).text == "English 3"
    assert M.find_label(tl, "What is the area of English 1?", loose=True).text == "English 1"
    m = TQ._RANGE_Q.search("What is the total floor area of English 1 to English 5?")
    assert m and m.group(2) == "1" and m.group(5) == "5"
    m = TQ._RANGE_Q.search("Combined area of Bedroom 2 and Bedroom 3")
    assert m and m.group(3).lower() == "and"


def test_kill_switch_writes_no_cache(tmp_path, monkeypatch):
    """A run with STOA_O1_PHOTO=0 must not leave a 'not a photo' sidecar that a later run (switch on) would trust."""
    sheet = _sheet()
    ph, _ = _photo(sheet, seed=7)
    src = tmp_path / "ph3.jpg"
    cv2.imwrite(str(src), ph)
    monkeypatch.setenv("STOA_O1_PHOTO", "0")
    assert P.cached_deskew(src, tmp_path) is None
    assert not (tmp_path / "ph3.photo.json").exists()
    monkeypatch.setenv("STOA_O1_PHOTO", "1")
    assert P.cached_deskew(src, tmp_path) is not None


def test_named_paper_and_no_dpi_tag(tmp_path):
    from PIL import Image

    sheet = _sheet()
    ph, _ = _photo(sheet, seed=11)
    src = tmp_path / "ph4.jpg"
    cv2.imwrite(str(src), ph)
    d = P.deskew(src, out_dir=tmp_path)
    with Image.open(d.path) as im:
        assert im.info.get("dpi") is None  # no tag: readers must not take the A3 prior as the sheet
    hit = P.named_paper(d.path, ["Plan", "Scale 1:100 @ A1"])
    assert hit is not None and hit[1] == "A1" and abs(hit[0] - max(d.size) / (841 / 25.4)) < 1e-6
    assert P.named_paper(d.path, ["Scale 1:100"]) is None
    assert P.a3_prior(d.path)[1] == "A3"


def test_aspect_single_axis_tilt():
    """W6 19:42: a camera tilted about ONE axis (one vanishing point at infinity) must still give the true aspect."""
    import math

    import numpy as np

    from stoa.solvers.raster import photo as P

    W, H = 4000, 3000
    f = 26 / 43.27 * math.hypot(W, H)

    def proj(tx):
        w, h = 2.0, 2.0 / 1.4142
        pts = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0], [-w / 2, h / 2, 0]])
        a = math.radians(tx)
        R = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
        X = (R @ pts.T).T + np.array([0, 0, 3.2])
        return np.stack([f * X[:, 0] / X[:, 2] + W / 2, f * X[:, 1] / X[:, 2] + H / 2], 1)

    for tx in (10, 20, 30):
        r = P._aspect(proj(tx), W, H)
        assert abs(r - 1.4142) < 0.01 and P._snap(r)[0] == "ISO A"
