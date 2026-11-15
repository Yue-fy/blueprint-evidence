"""F6 trust map: synthetic sheets (clean vs degraded picture, vector PDF), the router, path safety."""

from __future__ import annotations

import numpy as np
import pytest

from stoa.features import trust_map as TM


def _plan(noisy: bool, seed: int = 0) -> np.ndarray:
    """A 1600x1100 'plan': a building of 3x2 closed rooms with thick walls; noisy = faded, broken, grainy."""
    import cv2

    rng = np.random.default_rng(seed)
    g = np.full((1100, 1600), 255, np.uint8)
    x0, y0, x1, y1 = 250, 200, 1350, 900
    for x in np.linspace(x0, x1, 4).astype(int):
        cv2.line(g, (int(x), y0), (int(x), y1), 0, 9)
    for y in np.linspace(y0, y1, 3).astype(int):
        cv2.line(g, (x0, int(y)), (x1, int(y)), 0, 9)
    cv2.putText(g, "KITCHEN", (330, 400), cv2.FONT_HERSHEY_SIMPLEX, 1.2, 0, 2)
    if noisy:
        g = cv2.GaussianBlur(g, (5, 5), 1.4).astype(np.float32)
        g = 255 - (255 - g) * 0.22                       # faded toner (real scans: ink contrast 50-70 of 255)
        for _ in range(120):                              # broken walls
            cx, cy = rng.integers(x0, x1), rng.integers(y0, y1)
            g[cy - 14:cy + 14, cx - 14:cx + 14] = 250
        g += rng.normal(0, 6, g.shape)                     # grain
        specks = rng.random(g.shape) < 0.004
        g[specks] = 40
        g = np.clip(g, 0, 255).astype(np.uint8)
    return g


def _save(tmp_path, arr, name):
    from PIL import Image

    p = tmp_path / name
    Image.fromarray(arr).save(p)
    return str(p)


def test_clean_picture_reads_better_than_degraded(tmp_path):
    clean = TM.trust_map(_save(tmp_path, _plan(False), "clean.png"), ocr="off")
    bad = TM.trust_map(_save(tmp_path, _plan(True), "bad.png"), ocr="off")
    assert clean["ok"] and bad["ok"] and clean["kind"] == "raster"
    cs, bs = clean["summary"], bad["summary"]
    assert cs["green"] + cs["amber"] + cs["red"] in (99, 100, 101)
    # a clean picture without a scale is amber (scale unknown), not red; the degraded copy has much more red
    assert cs["red"] <= 10, cs
    assert bs["red"] >= cs["red"] + 30, (cs, bs)
    assert clean["scale"]["status"] == "unknown"
    assert any("scale" in r for t in clean["tiles"] for r in t.get("reasons", []))
    assert clean["cost"]["llm_tokens_in"] == 0 and clean["cost"]["api_calls"] == 0
    # sheet-level warning (tile share of red): the degraded sheet gets one plain sentence, the clean one none
    assert cs["warning"] is None and cs["red_tiles"] < TM.POOR_TILE_RED
    assert bs["red_tiles"] >= TM.POOR_TILE_RED and "reads poorly" in bs["warning"] and "upload" in bs["warning"]
    # a cached map carries it too
    assert TM.trust_map(_save(tmp_path, _plan(True), "bad.png"), ocr="off")["summary"]["warning"] == bs["warning"]


def test_visitor_scale_lifts_scale_only_amber(tmp_path):
    p = _save(tmp_path, _plan(False), "clean2.png")
    before = TM.trust_map(p, ocr="off")
    after = TM.trust_map(p, ocr="off", user_scale="1:200")
    assert before["scale"]["status"] == "unknown" and after["scale"]["status"] == "known"
    assert after["scale"]["source"] == "user" and "set by you (1:200)" in after["summary"]["scale"]
    only_scale = [t for t in before["tiles"] if t["level"] == "amber" and t.get("clean")]
    assert only_scale, "a clean picture with no scale has scale-only amber tiles"
    by_rc = {(t["col"], t["row"]): t for t in after["tiles"]}
    assert all(by_rc[(t["col"], t["row"])]["level"] == "green" for t in only_scale)
    assert after["summary"]["green"] > before["summary"]["green"]
    assert not any("scale" in r for t in after["tiles"] for r in t.get("reasons", []))
    # degraded regions stay what they were: the scale only lifts the cap
    assert {t["level"] for t in after["tiles"] if not t.get("clean") and t["level"] != "empty"} <= {"amber", "red"}
    # the plain map is unchanged (separate cache entries)
    assert TM.trust_map(p, ocr="off")["scale"]["status"] == "unknown"


def test_vector_pdf_is_green_with_printed_scale(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    pg = doc.new_page(width=842, height=595)
    for x in (150, 350, 550, 750):
        pg.draw_line((x, 100), (x, 450), width=4)
    for y in (100, 275, 450):
        pg.draw_line((150, y), (750, y), width=4)
    pg.insert_text((200, 200), "KITCHEN", fontsize=12)
    pg.insert_text((600, 540), "GROUND FLOOR PLAN  SCALE 1:100", fontsize=10)
    p = tmp_path / "plan.pdf"
    doc.save(p)
    m = TM.trust_map(str(p))
    assert m["kind"] == "vector" and m["space"] == "pdf_pt"
    assert m["scale"]["status"] == "known" and m["scale"]["ratio"] == 100
    assert m["summary"]["green"] >= 80, m["summary"]
    # tiles are in pdf_pt and cover the page
    assert max(t["bbox"][2] for t in m["tiles"]) == pytest.approx(842, abs=1)
    # level_at: the kitchen label's box is in a scored tile
    assert TM.level_at(m, [195, 188, 260, 202]) in ("green", "amber")


def test_resolve_rejects_unsafe_paths(tmp_path, monkeypatch):
    for bad in ("../etc/passwd", "/etc/passwd", "upload/xyz/a.png", "upload/0123456789ab/.hidden"):
        with pytest.raises(ValueError):
            TM.resolve(bad)
    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    d = tmp_path / "0123456789ab"
    d.mkdir()
    (d / "a.png").write_bytes(b"x")
    assert TM.resolve("upload/0123456789ab/a.png") == str(d / "a.png")
    with pytest.raises(FileNotFoundError):
        TM.resolve("upload/0123456789ab/b.png")


def test_router_fails_closed(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    d = tmp_path / "0123456789ab"
    d.mkdir()
    (d / "broken.png").write_bytes(b"not a png")
    _save(d, _plan(False), "plan.png")
    app = FastAPI()
    app.include_router(TM.router)
    c = TestClient(app)
    r = c.get("/feat/trust_map", params={"path": "upload/0123456789ab/plan.png", "ocr": "off"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["path"] == "upload/0123456789ab/plan.png"
    r = c.get("/feat/trust_map", params={"path": "upload/0123456789ab/broken.png"})
    assert r.status_code == 200 and r.json()["ok"] is False   # fail closed: the UI shows nothing
    assert c.get("/feat/trust_map", params={"path": "../x"}).status_code == 400
    r = c.get("/feat/trust_map/png", params={"path": "upload/0123456789ab/plan.png", "ocr": "off", "side": 600})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_router_uses_the_visitors_scale(tmp_path, monkeypatch):
    """user_scale from the web, else a scale F7's corrections store remembered for this upload (when F7 is on)."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("STOA_UPLOADS", str(tmp_path))
    d = tmp_path / "abcdef012345"
    d.mkdir()
    _save(d, _plan(False, seed=3), "plan.png")
    app = FastAPI()
    app.include_router(TM.router)
    c = TestClient(app)
    q = {"path": "upload/abcdef012345/plan.png", "ocr": "off"}
    assert c.get("/feat/trust_map", params=q).json()["scale"]["status"] == "unknown"
    j = c.get("/feat/trust_map", params={**q, "user_scale": "1:50"}).json()
    assert j["scale"]["status"] == "known" and "1:50" in j["summary"]["scale"]
    try:
        from stoa.features import corrections as C
    except Exception:  # noqa: BLE001
        pytest.skip("F7 corrections not on this branch")
    monkeypatch.setenv("STOA_FEATURES", "trust_map,corrections")
    monkeypatch.setattr(C, "_printed_scale", lambda fid: None)  # no recognizer run in a unit test
    C.add("upload/abcdef012345/plan.png", "scale", {"ratio": "1:100", "page": 0})
    j = c.get("/feat/trust_map", params=q).json()
    assert j["scale"]["status"] == "known" and "1:100" in j["summary"]["scale"] and "remembered" in j["summary"]["scale"]
    monkeypatch.setenv("STOA_FEATURES", "trust_map")  # F7 off: our own reading again
    assert c.get("/feat/trust_map", params=q).json()["scale"]["status"] == "unknown"


def _phone_photo(sheet: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The sheet printed and photographed: tilted with perspective on a dark table, light falling off to one side.
    Returns (RGB photo, the paper's corners in the photo)."""
    import cv2

    h, w = sheet.shape
    W, H = 2600, 2000
    quad = np.float32([[260, 220], [2330, 300], [2280, 1830], [200, 1700]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]]), quad)
    paper = cv2.warpPerspective(np.full_like(sheet, 255), M, (W, H), borderValue=0)
    ink = cv2.warpPerspective(sheet, M, (W, H), borderValue=255)
    g = np.where(paper > 0, ink, 70).astype(np.float32)               # dark table around the sheet
    light = np.linspace(1.0, 0.72, W, dtype=np.float32)[None, :]      # uneven light across the photo
    g = np.clip(g * light, 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2RGB), quad


def test_phone_photo_is_read_on_its_flat_copy(tmp_path):
    """t4: a phone photo of a clean print is measured on O1's deskewed copy (what the answers read), not on the raw
    photo whose table and light fall-off read as faint lines; tiles come back as quads on the photo."""
    photo, quad = _phone_photo(_plan(False))
    p = _save(tmp_path, photo, "phone.jpg")
    if TM._photo(p) is None:
        pytest.skip("O1's photo deskew is not available / did not detect the synthetic sheet")
    m = TM.trust_map(p, ocr="off")
    assert m["ok"] and m["photo"] and m["size"] == [2600, 2000]
    s = m["summary"]
    assert s["red"] <= 10, s                       # the clean print reads cleanly once flattened
    assert s["photo"] and s["warning"] == TM.PHOTO_WARNING
    used = [t for t in m["tiles"] if t["level"] != "empty"]
    assert used and all(len(t["poly"]) == 4 for t in used)
    # every tile lies on the paper (nothing is coloured on the table around it)
    import cv2

    for t in used:
        for x, y in t["poly"]:
            assert cv2.pointPolygonTest(quad.reshape(-1, 1, 2), (float(x), float(y)), True) >= -12, (x, y)
    # level_at finds the quad holding a point
    t = used[len(used) // 2]
    cx = sum(x for x, _ in t["poly"]) / 4
    cy = sum(y for _, y in t["poly"]) / 4
    assert TM.level_at(m, [cx - 1, cy - 1, cx + 1, cy + 1]) == t["level"]
    # the app's picture-scale reading does not make a photo's scale 'known' (its pixel size rests on an assumed paper)
    assert TM.trust_map(p, ocr="off", app_scale="1 m = 51 px")["scale"]["status"] != "known"
    # with the deskew switched off (STOA_O1_PHOTO=0) the same photo is read as it is: no quads, no photo note
    import os

    os.environ["STOA_O1_PHOTO"] = "0"
    try:
        TM._CACHE.clear()
        raw = TM.trust_map(p, ocr="off")
    finally:
        os.environ.pop("STOA_O1_PHOTO", None)
        TM._CACHE.clear()
    assert not raw["photo"] and not raw["summary"]["photo"] and all("poly" not in t for t in raw["tiles"])


def _levels(m):
    return [t["level"] for t in m["tiles"]]


def test_transparent_background_reads_like_white_paper(tmp_path):
    """A CAD export with a transparent background (black RGB under alpha 0) is read on white, as the viewer shows it:
    read as it is, the whole sheet was one block of 'ink' and the map came out empty (0 / 0 / 0 %)."""
    g = _plan(False)
    rgba = np.dstack([np.zeros_like(g)] * 3 + [(255 - g).astype(np.uint8)])
    opaque = TM.trust_map(_save(tmp_path, g, "opaque.png"), ocr="off")
    clear = TM.trust_map(_save(tmp_path, rgba, "clear.png"), ocr="off")
    assert _levels(clear) == _levels(opaque)
    assert any("transparent" in n for n in clear["notes"]) and not clear["negative"]


def test_blueprint_is_measured_inverted_and_says_so(tmp_path):
    """White lines on blue (a cyanotype): measured as its inverse; the sheet note says it is a blueprint and gives no
    accuracy number (none measured)."""
    g = _plan(False).astype(np.float32)
    blue = np.dstack([25 + 0.8 * (255 - g), 60 + 0.7 * (255 - g), 140 + 0.45 * (255 - g)]).clip(0, 255).astype(np.uint8)
    pos = TM.trust_map(_save(tmp_path, _plan(False), "pos.png"), ocr="off")
    neg = TM.trust_map(_save(tmp_path, blue, "blueprint.png"), ocr="off")
    assert neg["negative"] and not pos["negative"]
    assert neg["summary"]["negative"] and neg["summary"]["warning"] == TM.NEGATIVE_WARNING
    assert "%" not in TM.NEGATIVE_WARNING and "in 10" not in TM.NEGATIVE_WARNING
    # the same drawing reads about the same either way round (was: lines lighter than the paper = no ink found)
    assert neg["summary"]["red"] <= 10 and abs(neg["summary"]["clean"] - pos["summary"]["clean"]) <= 15, (neg["summary"], pos["summary"])
    # a light sheet photographed on a black backdrop is NOT a blueprint (the bright part is a block, not lines)
    dark = np.full((1100, 1600), 20, np.uint8)
    dark[250:850, 400:1200] = 235
    assert not TM._is_negative(dark)
    # a dim photo of a normal print: dark paper, darker lines
    assert not TM._is_negative((_plan(False) * 0.35 + 10).astype(np.uint8))


def test_blank_page_says_no_drawing(tmp_path):
    m = TM.trust_map(_save(tmp_path, np.full((900, 1300), 250, np.uint8), "blank.png"), ocr="off")
    assert m["ok"] and m["summary"]["line"].startswith("No drawing found")
