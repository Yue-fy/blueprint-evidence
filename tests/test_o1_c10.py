"""O1 c10: sticky text layer per file, small OCR reads behind a busy server, dimension prints are no areas."""
import json

from stoa.solvers.raster import textlayer as T
from stoa.solvers.raster import textqa as Q


def _layer(p, texts):
    return T.TextLayer(path=p, size=(2400, 1600),
                       items=[T.TextItem(t, [60.0 * k, 10.0, 60.0 * k + 50, 30.0]) for k, t in enumerate(texts)])


def test_sticky_vlm_layer_after_an_ocr_budget_fallback(monkeypatch, tmp_path):
    from stoa import ocr

    img = tmp_path / "scan.png"
    from PIL import Image

    Image.new("L", (2400, 1600), 200).save(img)
    p = str(img)
    vlm = _layer(p, ["Kitchen", "Hall", "7,200"])
    ocr_tl = _layer(p, ["Kitchen", "Hall"])
    vlm_read = {"n": 0}

    def vlm_layer(*a, **k):
        vlm_read["n"] += 1
        return vlm

    monkeypatch.setattr(ocr, "CACHE_DIR", tmp_path / "ocr")
    monkeypatch.setattr(T, "ocr_suits", lambda path: True)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(T, "_vlm_layer", vlm_layer)
    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")

    def over_budget(path):
        raise RuntimeError("OCR over its 120 s budget (STOA_OCR_TIMEOUT); the server read continues")

    monkeypatch.setattr(ocr, "text_layer", over_budget)
    T._mem.clear()
    assert T.text_layer(p) is vlm  # first question: the OCR is not ready -> the VLM layer
    side = ocr._key(p).with_suffix(".layer")
    assert side.exists() and json.loads(side.read_text())["layer"] == "vlm"

    # the OCR lands later; a new process (memory cleared) keeps the VLM layer the first answers came from
    monkeypatch.setattr(ocr, "cached", lambda path: True)
    monkeypatch.setattr(ocr, "text_layer", lambda path: ocr_tl)
    monkeypatch.setattr(T, "_vlm_cached", lambda *a, **k: True)
    T._mem.clear()
    tl = T.text_layer(p)
    assert [i.text for i in tl.items] == [i.text for i in vlm.items] and "kept so later answers" in tl.notes[-1]
    assert f"{p}{T.file_tag(p)}|ocr" not in T._mem  # mark_ocr_exact never marks the VLM boxes exact

    monkeypatch.setenv("STOA_O1_STICKY", "0")
    monkeypatch.setattr(T, "STICKY", False)
    T._mem.clear()
    tl = T.text_layer(p)  # switched off: as before, the OCR reading + the cached VLM texts where it read nothing
    assert [i.text for i in tl.items][:2] == ["Kitchen", "Hall"] and "PP-OCR reading +" in tl.notes[-1]
    T._mem.clear()


def test_no_sticky_when_the_ocr_simply_fails(monkeypatch, tmp_path):
    from stoa import ocr

    p = str(tmp_path / "x.png")
    from PIL import Image

    Image.new("L", (2400, 1600), 200).save(p)
    monkeypatch.setattr(ocr, "CACHE_DIR", tmp_path / "ocr")
    monkeypatch.setattr(T, "ocr_suits", lambda path: True)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(T, "_vlm_layer", lambda *a, **k: _layer(p, ["A"]))
    monkeypatch.setenv("STOA_TEXT_ENGINE", "auto")

    def broken(path):
        raise RuntimeError("OCR engine failed: no model")

    monkeypatch.setattr(ocr, "text_layer", broken)
    T._mem.clear()
    T.text_layer(p)
    assert not ocr._key(p).with_suffix(".layer").exists()
    T._mem.clear()


def test_dimension_print_is_no_area():
    tl = _layer("x.png", ["Resistant Materials", "7,200", "7.200", "Circulation", "Store 6.85 m²"])
    assert Q._dim_printed(7.2, tl) and Q._dim_printed(7200, tl)
    assert not Q._dim_printed(6.85, tl)  # printed with m²
    assert not Q._dim_printed(104.0, tl)
    tl2 = _layer("x.png", ["7,200", "Lab 7.20 m²"])
    assert not Q._dim_printed(7.2, tl2)  # the same number IS printed as an area somewhere
    tl3 = _layer("x.png", ["English 9", "21.6"])
    assert not Q._dim_printed(21.6, tl3)  # one or two decimals: an area label may print that


def test_engine_writes_out_only_into_the_cache(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    import stoa.ocr as O

    spec = importlib.util.spec_from_file_location("o1_engine_c10", Path(O.__file__).with_name("engine.py"))
    try:
        E = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(E)
    except Exception:  # noqa: BLE001 - the engine imports paddle-side deps only inside functions; skip if not
        import pytest

        pytest.skip("engine module not importable in this env")
    shared = tmp_path / "shared"
    (shared / "artifacts" / "O1" / "ocr").mkdir(parents=True)
    monkeypatch.setenv("JUNCTION_SHARED", str(shared))
    d = {"size": [1, 1], "items": []}
    good = shared / "artifacts" / "O1" / "ocr" / "a.123.json"
    assert E._write_out(str(good), d) and json.loads(good.read_text()) == d
    assert not E._write_out(str(tmp_path / "evil.json"), d) and not (tmp_path / "evil.json").exists()
    assert not E._write_out(str(good.with_suffix(".txt")), d)
    assert not E._write_out(str(good), {"error": "x"})
