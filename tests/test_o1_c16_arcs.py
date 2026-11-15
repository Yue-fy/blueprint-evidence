"""O1 cycle 16: door-arc detector memory fix (identical detections)."""

from PIL import Image, ImageDraw


def test_door_arcs_threaded_fold_equals_sequential(tmp_path, monkeypatch):
    # O1 c16: the low-memory fold (uint16 pair index, buffers reused, results dropped once folded) must give exactly
    # the sequential loop's detections
    from stoa.solvers.raster import doorarcs as DA

    im = Image.new("L", (900, 700), 255)
    d = ImageDraw.Draw(im)
    for x, y in ((150, 150), (450, 300), (700, 520), (300, 560)):
        d.line([x, y, x + 120, y], fill=0, width=3)
        d.arc([x - 50, y - 50, x + 50, y + 50], 0, 90, fill=0, width=2)
    p = tmp_path / "arcs.png"
    im.save(p)
    monkeypatch.setattr(DA, "ARC_THREADS", 1)
    seq = DA.detect(str(p), 60.0)
    monkeypatch.setattr(DA, "ARC_THREADS", 4)
    par = DA.detect(str(p), 60.0)
    assert seq and seq == par
