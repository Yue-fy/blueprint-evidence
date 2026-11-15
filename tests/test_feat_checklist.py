"""F6 check list: risk ranking of takeoff rows / answers, totals without the unresolved rows, a person's ticks."""

from __future__ import annotations

import pytest

from stoa.features import checklist as C


def _row(i, item, q, v, unit, conf, ev=None, **kw):
    return {"id": f"r{i}", "section": "room", "item": item, "quantity": q, "value": v, "unit": unit,
            "confidence": conf, "status": "ok", "method": "pipeline/x", "evidence": ev or [], **kw}


BOX = {"file": "upload/x/a.pdf", "page": 0, "bbox": [10, 10, 40, 30], "bbox_space": "pdf_pt",
       "note": "Office = 12.5 m2 (printed)"}


def _st(rows):
    return {"status": "done", "job": "0123456789abcdef-p0-t2", "file": "upload/x/a.pdf", "page": 0, "rows": rows}


def test_risky_rows_first_and_out_of_totals():
    rows = [
        _row(1, "Office", "area", 12.5, "m2", 0.9, [BOX], trace=["support: supported — the printed value is marked"]),
        _row(2, "Store", "area", 6.4, "m2", 0.3, [BOX], review="rough reading (confidence 0.30)"),
        _row(3, "Hall", "area", 240.0, "m2", 0.45, [], trace=["support: unlocated — nothing on the drawing is marked"]),
        _row(4, "Lab", "area", 50.0, "m2", 0.85, [BOX], check={"agree": True}),
        _row(5, "Stair", "area", None, "m2", 0.0, [], status="timeout"),
    ]
    out = C.build(_st(rows), None, {}, 0)
    assert out["ok"] and out["status"] == "done"
    states = {it["title"]: it["state"] for it in out["items"]}
    assert states == {"Office": "ok", "Store": "check", "Hall": "check", "Lab": "ok", "Stair": "check"}
    assert [it["state"] for it in out["items"]][:3] == ["check"] * 3  # flagged first
    assert out["summary"]["check"] == 3 and out["summary"]["n"] == 5
    assert out["summary"]["line"].startswith("Check these 3 of 5 first")
    # 'rough reading (confidence …)' is not repeated as 'low confidence'
    store = next(it for it in out["items"] if it["title"] == "Store")
    assert not any(w.startswith("low confidence") for w in store["why"])
    area = next(t for t in out["totals"] if t["quantity"] == "area")
    assert area["value"] == 62.5 and area["rows"] == 2  # Office + Lab only
    assert area["waiting"] == 246.4 and area["waiting_rows"] == 2  # Store + Hall (Stair has no value)
    lab = next(it for it in out["items"] if it["title"] == "Lab")
    assert "two methods agree" in lab["good"]


def test_a_tick_puts_the_row_back_into_the_totals():
    rows = [_row(1, "Office", "area", 12.5, "m2", 0.9, [BOX], trace=["support: supported — x"]),
            _row(2, "Store", "area", 6.4, "m2", 0.3, [BOX], review="rough reading (confidence 0.30)")]
    out = C.build(_st(rows), None, {"r2": "23:10"}, 0)
    store = next(it for it in out["items"] if it["title"] == "Store")
    assert store["state"] == "checked"
    area = next(t for t in out["totals"] if t["quantity"] == "area")
    assert area["value"] == 18.9 and area["waiting_rows"] == 0
    assert "Nothing left to check" in out["summary"]["line"]


def test_disagreeing_methods_always_flag():
    r = _row(1, "Office", "area", 12.5, "m2", 0.95, [BOX], check={"agree": False, "value": 15.0},
             trace=["support: supported — x"])
    sc = C.score(r, "What is the floor area of the Office?", None)
    assert sc["flag"] and sc["why"][0] == "two methods disagree: 12.5 m² vs 15 m²"


def test_signals_from_trace_and_trust_map(monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "trust_map,checklist")
    tmap = {"tiles": [{"bbox": [0, 0, 100, 100], "level": "red"}], "scale": {"status": "unknown"},
            "summary": {"warning": "This sheet reads poorly."}}
    item = {"value": 7.0, "unit": "m2", "confidence": 0.6, "evidence": [BOX],
            "trace": ["support: approximate — traced outline"]}
    sig = C.signals(item, "What is the area of the store?", tmap, 0)
    assert sig["colour"] == "red" and sig["support"] == "approximate"
    x = sig["x"]
    assert x["red"] and x["scale"] and x["poor"] and x["mid"] and x["area"] and not x["low"]
    assert 0 < C.p_wrong(x) < 1
    why, _good = C.reasons(sig)
    assert why and any("red" in w for w in why)


def test_ticks_are_per_visitor(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_CHECKLIST_DIR", str(tmp_path))
    C.set_check("upload/x/a.pdf", 0, "visA", "r2", True)
    assert "r2" in C.load_checks("upload/x/a.pdf", 0, "visA")
    assert C.load_checks("upload/x/a.pdf", 0, "visB") == {}
    C.set_check("upload/x/a.pdf", 0, "visA", "r2", False)
    assert C.load_checks("upload/x/a.pdf", 0, "visA") == {}
    with pytest.raises(ValueError):
        C.set_check("upload/x/a.pdf", 0, "visA", "../etc", True)


def test_router_off_and_fail_closed(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()
    app.include_router(C.router)
    cl = TestClient(app)
    monkeypatch.setenv("STOA_FEATURES", "")
    assert cl.get("/feat/checklist", params={"path": "upload/x/a.pdf"}).status_code == 404
    monkeypatch.setenv("STOA_FEATURES", "checklist")
    r = cl.get("/feat/checklist", params={"path": "../../etc/passwd"})
    assert r.status_code == 200 and r.json()["ok"] is False  # fail closed, no traceback
    r = cl.post("/feat/checklist/answers", json={"path": "../../etc/passwd", "answers": []})
    assert r.json()["ok"] is False


def test_trust_map_scale_is_per_visitor(tmp_path, monkeypatch):
    """F7 22:13: a scale visitor A set (F7 memory) shows in A's trust map only, never in B's or a visitor-less one."""
    from stoa.features import corrections as F7
    from stoa.features import trust_map as TM

    root = tmp_path / "uploads"
    (root / "abcdef012345").mkdir(parents=True)
    (root / "abcdef012345" / "a.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_CORRECTIONS_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("STOA_FEATURES", "corrections,trust_map")
    monkeypatch.setattr(F7, "_printed_scale", lambda fid: None)
    F7._CACHE.clear()
    rel = "upload/abcdef012345/a.pdf"
    F7.add(rel, "scale", {"ratio": "1:200", "visitor": "visA"})
    a = TM._user_scale(None, rel, {"status": "unknown"}, "visA")
    assert a and a["status"] == "known" and "1:200" in a["why"]
    assert TM._user_scale(None, rel, {"status": "unknown"}, "visB") is None
    assert TM._user_scale(None, rel, {"status": "unknown"}, None) is None


def _sq(x, y, w=100, h=100):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def _room(i, item, copy, x, y, windows, room_type="teaching", w=100):
    """A 7 x 7 m room drawn 100 x 100 units (≈ 14.3 units per metre) + its 'Windows' row (no marks when 0)."""
    poly = _sq(x, y, w, 100)
    ev = [{"page": 0, "bbox": [x, y, x + w, y + 100], "bbox_space": "pdf_pt", "polygon": poly}]
    kw = {"copy": copy, "copies": 2, "room_type": room_type}
    return [_row(i, item, "area", round(49.0 * w / 100, 2), "m2", 0.9, ev, **kw),
            _row(i + 100, item, "windows", windows, "count", 0.55, ev if windows else [], **kw)]


def test_zero_windows_listed_only_on_a_facade_and_rows_zoom_to_their_room():
    # a 3 x 3 block of rooms (walls 0.4 m = 6 units): the middle one has no outside wall
    rows, i = [], 1
    for cx in range(3):
        for cy in range(3):
            x, y = cx * 106, cy * 106
            if (cx, cy) == (1, 1):
                rows += _room(i, "Group Room", 2, x, y, 0)  # interior: no window expected
            elif (cx, cy) == (0, 0):
                rows += _room(i, "Group Room", 1, x, y, 0)  # facade, its twin elsewhere has a window
            elif (cx, cy) == (2, 2):
                rows += _room(i, "Group Room", 3, x, y, 2)
            elif (cx, cy) == (2, 0):
                rows += _room(i, "Store", 1, x, y, 0, room_type="storage")  # facade, but a store
            else:
                rows += _room(i, f"Office {i}", 1, x, y, 1, room_type="staff")
            i += 1
    geo = C.rooms_geometry(rows)
    assert geo[("Group Room", 2)]["facade_m"] == 0 and geo[("Group Room", 1)]["facade_m"] >= C.FACADE_MIN_M
    out = C.build(_st(rows), None, {}, 0)
    by = {(it["title"], it["what"]): it for it in out["items"]}
    gr1 = by[("Group Room (1)", "Windows")]
    assert gr1["state"] == "check" and gr1["why"][0].startswith("on an outside wall")
    assert "Group Room (3) has one" in gr1["why"][1]
    assert by[("Group Room (2)", "Windows")]["state"] == "ok"
    assert by[("Group Room (2)", "Windows")]["good"][0] == "no outside wall: no window expected"
    assert by[("Store (1)", "Windows")]["state"] == "ok"
    # a row with no mark of its own zooms to its room's outline
    assert gr1["where"] == "room" and gr1["zoom"] == [0, 0, 100, 100]
    assert by[("Group Room (3)", "Windows")]["where"] == "marks"


def test_disagreement_reason_names_both_numbers():
    r = _row(1, "TUALETTRUUM 5-3", "area", 63.82, "m2", 0.8, [BOX],
             method="IfcSpace outline drawn on the sheet (the model's own room)",
             check={"value": 59.1, "diff_pct": -7.4, "agree": False, "how": "traced from the wall lines (lineplan)"})
    sc = C.score(r, "What is the floor area of TUALETTRUUM 5-3?", None, 0)
    assert sc["flag"] and sc["why"][0] == "the IFC model says 63.82 m², the walls traced 59.1 m² (-7.4 %)"


def test_rows_listed_only_for_the_scale_are_named_and_a_contradicting_scale_lists_every_size():
    tm_unknown = {"scale": {"status": "unknown"}, "summary": {}, "tiles": []}
    rows = [_row(i, f"Room {i}", "baseboard", 10.0 + i, "m", 0.45, [BOX]) for i in range(1, 7)]
    st = dict(_st(rows), scale_n=200.0)
    out = C.build(st, tm_unknown, {}, 0)
    assert out["summary"]["check"] == 6 and out["summary"]["only_scale"] == 6
    assert out["summary"]["hint"].startswith("6 of them are listed only because the scale is not confirmed")
    # the visitor confirms 1:200 -> the trust map knows the scale -> those rows leave the list
    tm_user = {"scale": {"status": "known", "source": "user", "ratio": None}, "summary": {}, "tiles": []}
    out = C.build(st, tm_user, {}, 0, C.user_ratio(tm_user, "1:200.02"))
    assert out["summary"]["check"] == 0 and out["summary"]["hint"] is None
    # ... but 1:100 contradicts the takeoff's 1:200: every size is listed with the factor
    out = C.build(st, tm_user, {}, 0, C.user_ratio(tm_user, "1:100"))
    assert out["summary"]["check"] == 6
    assert out["items"][0]["why"][0].startswith("measured at 1:200, but you set 1:100: this value would be ×0.50")


def test_a_picture_scale_in_units_per_metre_is_compared_with_the_takeoffs():
    rows = []
    for i in range(4):
        rows += _room(1 + i, f"Office {i}", 1, i * 106, 0, 1, room_type="staff")
    st = dict(_st(rows), scale_n=200.0)
    upm, space = C.takeoff_upm(rows)
    assert abs(upm - 100 / 7) < 0.01 and space == "pdf_pt"
    tm = {"scale": {"status": "known", "source": "user"}, "summary": {}, "tiles": []}
    same = C.build(st, tm, {}, 0, None, (upm * 1.01, "pdf_pt"))
    assert not any(it["why"] and it["why"][0].startswith("measured at") for it in same["items"])
    double = C.build(st, tm, {}, 0, None, (upm * 2, "pdf_pt"))
    area = next(it for it in double["items"] if it["what"] == "Floor area")
    assert area["state"] == "check" and "this value would be ×0.25" in area["why"][0]


def test_the_pictures_own_scale_confirms_the_takeoffs_when_they_agree():
    rows = []
    for i in range(4):
        rows += _room(1 + i, f"Office {i}", 1, i * 106, 0, 1, room_type="staff")
    for r in rows:  # a picture: evidence in image pixels, sizes only fairly sure, outlines traced
        r["confidence"] = 0.45
        r["trace"] = ["support: approximate — traced outline"]
        for e in r["evidence"]:
            e["bbox_space"] = "image_px"
    st = dict(_st(rows), scale_n=200.0)
    tm = {"scale": {"status": "unknown"}, "summary": {}, "tiles": []}
    upm = C.takeoff_upm(rows)[0]
    before = C.build(st, tm, {}, 0)
    assert before["summary"]["only_scale"] >= 4
    agree = C.build(st, tm, {}, 0, drawing_upm=(upm * 1.02, "image_px"))
    assert agree["scale"] == "known" and "agree (within 2 %)" in agree["scale_why"] and agree["summary"]["only_scale"] == 0
    assert agree["summary"]["check"] < before["summary"]["check"]
    far = C.build(st, tm, {}, 0, drawing_upm=(upm * 1.2, "image_px"))  # two readings that differ prove nothing
    assert far["scale"] == "unknown"
    pdf = C.build(st, tm, {}, 0, drawing_upm=(upm, "pdf_pt"))  # on a PDF the app's scale may be a guess
    assert pdf["scale"] == "unknown"


def test_measure_saves_an_area_row_as_takeoffs_fix(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from stoa.features import takeoff as T

    monkeypatch.setenv("STOA_FEATURES", "checklist")
    rows = [_row(1, "Store", "area", 6.4, "m2", 0.3, [BOX], review="rough reading (confidence 0.30)"),
            _row(2, "Store", "doors", 1, "count", 0.7, [BOX])]
    fixes = {}
    monkeypatch.setattr(T, "resolve", lambda p: ("/x/a.pdf", "upload/x/a.pdf"))
    monkeypatch.setattr(T, "job_key", lambda a, p: "0123456789abcdef-p0-t2")
    (tmp_path / "0123456789abcdef-p0-t2").mkdir()
    monkeypatch.setattr(T, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("STOA_CHECKLIST_DIR", str(tmp_path / "ticks"))

    def status(key, virtual=None):
        rs = [dict(r, value=fixes[r["id"]], fixed="23:20", review=None) if r["id"] in fixes else r for r in rows]
        return _st(rs)

    def fix(key, rid, value, unit=None, polygon=None, space=None, item=None):
        fixes[rid] = value
        return status(key)

    monkeypatch.setattr(T, "status", status)
    monkeypatch.setattr(T, "fix", fix)
    monkeypatch.setattr(C, "_tmap", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(C.router)
    c = TestClient(app)
    bad = c.post("/feat/checklist/measure", json={"path": "upload/x/a.pdf", "id": "r2", "value": 3}).json()
    assert bad["ok"] is False and not fixes  # a door count is not measured as an area
    out = c.post("/feat/checklist/measure", json={"path": "upload/x/a.pdf", "id": "r1", "value": 6.9,
                                                  "polygon": [[0, 0], [1, 0], [1, 1]], "space": "pdf_pt"}).json()
    assert fixes == {"r1": 6.9}
    store = next(it for it in out["items"] if it["id"] == "r1")
    assert store["state"] == "checked" and store["checked"] == "measured 23:20" and store["value"] == 6.9
    area = next(t for t in out["totals"] if t["quantity"] == "area")
    assert area["value"] == 6.9 and area["waiting_rows"] == 0


def test_windows_in_a_room_with_no_outside_wall_are_listed():
    rows, i = [], 1
    for cx in range(3):
        for cy in range(3):
            interior = (cx, cy) == (1, 1)
            rows += _room(i, "WC" if interior else f"Office {i}", 1, cx * 106, cy * 106, 2 if interior else 1,
                          room_type="wet" if interior else "staff")
            i += 1
    for r in rows:  # one mark per counted window: the model alone would not list them
        r["trace"] = ["support: supported — one mark per item"]
    out = C.build(_st(rows), None, {}, 0)
    wc = next(it for it in out["items"] if it["title"].startswith("WC") and it["what"] == "Windows")
    assert wc["state"] == "check" and wc["why"][0].startswith("2 windows found, but the room has no outside wall")
    assert all(it["state"] == "ok" for it in out["items"] if it["what"] == "Windows" and not it["title"].startswith("WC"))


def test_a_model_value_with_a_close_trace_is_not_forced_onto_the_list():
    ifc = "IfcSpace outline drawn on the sheet (the model's own room)"
    close = _row(1, "ESIK 36-2", "area", 3.01, "m2", 0.8, [BOX], method=ifc,
                 check={"value": 3.08, "diff_pct": 2.3, "agree": False, "how": "traced from the wall lines"},
                 trace=["support: supported — x"])
    far = _row(2, "ESIK 37-2", "area", 3.01, "m2", 0.8, [BOX], method=ifc,
               check={"value": 3.4, "diff_pct": 13.0, "agree": False, "how": "traced from the wall lines"},
               trace=["support: supported — x"])
    assert not C.score(close, "area", None)["flag"]
    sc = C.score(far, "area", None)
    assert sc["flag"] and sc["why"][0].startswith("the IFC model says 3.01 m², the walls traced 3.4 m² (+13.0 %)")


def test_measure_is_kept_per_visitor_when_the_takeoff_supports_it(monkeypatch, tmp_path):
    """F2 1ef28b65: takeoff fixes are per visitor. The check list passes the browser's id to status / fix when the takeoff
    takes it, so a judge's Measure shows in THEIR list only; an older takeoff (no visitor parameter) still works."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from stoa.features import takeoff as T

    monkeypatch.setenv("STOA_FEATURES", "checklist")
    rows = [_row(1, "Store", "area", 6.4, "m2", 0.3, [BOX], review="rough reading (confidence 0.30)")]
    fixes: dict = {}
    monkeypatch.setattr(T, "resolve", lambda p: ("/x/a.pdf", "upload/x/a.pdf"))
    monkeypatch.setattr(T, "job_key", lambda a, p: "0123456789abcdef-p0-t2")
    (tmp_path / "0123456789abcdef-p0-t2").mkdir()
    monkeypatch.setattr(T, "cache_dir", lambda: tmp_path)
    monkeypatch.setenv("STOA_CHECKLIST_DIR", str(tmp_path / "ticks"))

    def status(key, virtual=None, visitor=None):
        mine = fixes.get(visitor or "anon", {})
        return _st([dict(r, value=mine[r["id"]], fixed="00:20", review=None) if r["id"] in mine else r for r in rows])

    def fix(key, rid, value, unit=None, polygon=None, space=None, item=None, visitor=None):
        fixes.setdefault(visitor or "anon", {})[rid] = value
        return status(key, visitor=visitor)

    monkeypatch.setattr(T, "status", status)
    monkeypatch.setattr(T, "fix", fix)
    monkeypatch.setattr(C, "_tmap", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(C.router)
    c = TestClient(app)
    a, b = "a1b2c3d4e5f6a7b8c9d0e1f2", "ffffffffffffffffffffffff"
    out = c.post("/feat/checklist/measure", json={"path": "upload/x/a.pdf", "id": "r1", "value": 6.9, "visitor": a}).json()
    assert fixes == {a: {"r1": 6.9}}
    assert next(it for it in out["items"] if it["id"] == "r1")["state"] == "checked"
    other = c.get("/feat/checklist", params={"path": "upload/x/a.pdf", "visitor": b}).json()
    assert next(it for it in other["items"] if it["id"] == "r1")["state"] != "checked"  # the next judge: not theirs
    # a bad id is never passed on (F2 raises on it): the request falls back to the visitor-less read
    assert C._vkw(status, "x") == {} and C._vkw(status, a) == {"visitor": a}
    assert C._vkw(lambda key, virtual=None: None, a) == {}  # an older takeoff without the parameter


def test_start_over_forgets_only_this_visitors_ticks(tmp_path, monkeypatch):
    """'Start over' (stoa:reset with forgetCorrections) drops one visitor's ticks on every page; other visitors and the
    shared anon file stay; a missing / bad id forgets nothing."""
    monkeypatch.setenv("STOA_CHECKLIST_DIR", str(tmp_path))
    me, other = "visitorA123", "visitorB456"
    C.set_check("upload/x/a.pdf", 0, me, "r2", True)
    C.set_check("upload/x/a.pdf", 3, me, "r5", True)
    C.set_check("upload/x/a.pdf", 0, other, "r2", True)
    C.set_check("upload/x/a.pdf", 0, None, "r7", True)
    assert C.forget_checks("upload/x/a.pdf", None) == 0
    assert C.forget_checks("upload/x/a.pdf", "../etc") == 0
    assert C.forget_checks("upload/x/a.pdf", me) == 2
    assert C.load_checks("upload/x/a.pdf", 0, me) == {} and C.load_checks("upload/x/a.pdf", 3, me) == {}
    assert "r2" in C.load_checks("upload/x/a.pdf", 0, other)
    assert "r7" in C.load_checks("upload/x/a.pdf", 0, None)


def test_answers_are_flagged_by_confidence_alone():
    """A visitor's own answers: flagged when the reader is unsure (< ANSWER_FLAG_CONF), risk = 1 - confidence; the other
    signals (no mark, unknown scale, a red region) are reasons, not a second vote (measured: the plain rule beat the
    12-signal model on d30, d31 and T1-calibrated runs)."""
    unsure = {"value": 12.0, "unit": "m2", "confidence": 0.42, "evidence": []}
    sure_but_unmarked = {"value": 12.0, "unit": "m2", "confidence": 0.8, "evidence": []}
    a = C.score(unsure, "How big is the store?", None, answer=True)
    b = C.score(sure_but_unmarked, "How big is the store?", None, answer=True)
    assert a["flag"] and a["risk"] == 0.58 and a["only"] is None
    assert not b["flag"] and b["risk"] == 0.2
    assert any("mark" in w or "confidence" in w for w in a["why"])
    # the takeoff rows keep the model: the same unmarked, fairly-sure row is scored by p_wrong, not by 1 - confidence
    assert C.score(sure_but_unmarked, "How big is the store?", None)["risk"] != 0.2


def test_calibrated_confidence_uses_t1s_unsure_line():
    """T1's calibrated confidence (trace 'calibration (T1) …'): 'unsure' = <= 0.6 (its doubt cap sets exactly 0.6); a raw
    0.6 is not flagged (on raw confidence <= 0.6 would list half the answers)."""
    cal = {"value": 30.0, "unit": "m2", "confidence": 0.6, "evidence": [],
           "trace": ["route: plan_rooms", "calibration (T1): 0.45 -> 0.60 (P(right) …)"]}
    raw = {"value": 30.0, "unit": "m2", "confidence": 0.6, "evidence": [], "trace": ["route: plan_rooms"]}
    assert C.score(cal, "How big is the hall?", None, answer=True)["flag"]
    assert not C.score(raw, "How big is the hall?", None, answer=True)["flag"]
    assert not C.score(dict(cal, confidence=0.77), "How big is the hall?", None, answer=True)["flag"]


def test_colour_reasons_name_no_colour_when_the_trust_map_is_off(monkeypatch):
    """The captain's demo subset has the check list but not the trust map: a reason must not send the visitor looking
    for a red region nobody can see."""
    tmap = {"tiles": [{"bbox": [0, 0, 100, 100], "level": "red"}], "scale": {"status": "printed"}, "summary": {}}
    item = {"value": 7.0, "unit": "m2", "confidence": 0.6, "evidence": [BOX], "trace": ["support: approximate — x"]}
    sig = C.signals(item, "What is the area of the store?", tmap, 0)
    monkeypatch.setenv("STOA_FEATURES", "privacy,region,takeoff,checklist,report")
    why, _ = C.reasons(sig)
    assert any("hard to read" in w for w in why) and not any("(red)" in w for w in why)
    monkeypatch.setenv("STOA_FEATURES", "all")
    why, _ = C.reasons(sig)
    assert any("(red) region" in w for w in why)


def test_the_calibration_sentence_matches_the_builds_confidence(monkeypatch):
    """d32+ answers carry T1's calibrated confidence and are listed at <= 0.6: the panel quotes the numbers measured with
    that rule; an older build (no stoa/trust/calibrate.py) or STOA_T1_CALIBRATE=0 quotes the raw < 0.5 numbers."""
    monkeypatch.setattr(C, "_t1_build", lambda: True)
    assert f"({C.CALIBRATION_T1['n']} answers" in C.calib_line()
    monkeypatch.setattr(C, "_t1_build", lambda: False)
    assert f"({C.CALIBRATION['n']} answers" in C.calib_line()
    monkeypatch.undo()
    monkeypatch.setenv("STOA_T1_CALIBRATE", "0")
    assert C._t1_build() is False


def test_piping_plan_equipment_counts_join_the_list():
    """F2's piping-plan takeoff rows (section 'equipment': tagged equipment per family) are on the list, one mark per tag
    (Stoa's M-101A said 'the takeoff has no readings' before); on a picture they carry the OCR caveat."""
    marks = [{"file": "upload/x/a.pdf", "page": 0, "bbox": [10 + 20 * i, 10, 25 + 20 * i, 20], "bbox_space": "pdf_pt",
              "note": f"CV-{i + 1}"} for i in range(5)]
    row = {"id": "e1", "section": "equipment", "item": "Check valves (CV-n)", "quantity": "count", "unit": "count",
           "value": 5, "confidence": 0.9, "status": "ok", "method": "printed equipment tags (piping reader, PDF text)",
           "evidence": marks}
    st = {"status": "done", "rows": [row], "job": "j", "file": "upload/x/a.pdf", "page": 0}
    out = C.build(st, None)
    it = out["items"][0]
    assert it["title"] == "Check valves (CV-n)" and it["what"] == "Count" and it["state"] == "ok"
    assert it["support"] == "supported" and it["zoom"] and "note" not in it
    assert "low-risk" in out["summary"]["line"]
    pic = dict(row, confidence=0.75, _px=True, method="printed equipment tags (piping reader, the picture's OCR layer)")
    out = C.build({**st, "rows": [pic]}, None)
    assert out["items"][0]["note"] == C.OCR_TAG_NOTE and out["items"][0]["state"] == "ok"


def test_equipment_counts_are_listed_by_how_they_were_counted():
    """Distinct IDs with a gap in the numbering (PS-2 missing between PS-1 and PS-5) are listed on any sheet; a count
    of EVERY printed label is listed when OCR read it from a picture (one missed label changes it), not on a PDF."""
    base = {"section": "equipment", "quantity": "count", "unit": "count", "status": "ok", "evidence": []}
    gap = dict(base, id="g", item="Pipe support types (PS-n)", value=4, confidence=0.9,
               assumptions=["counted distinct IDs: PS-1, PS-3, PS-4, PS-5"], method="printed equipment tags (PDF text)")
    every_pdf = dict(base, id="e", item="Pressure gauges (PG)", value=21, confidence=0.9,
                     assumptions=["counted every printed symbol label: 21 x PG"], method="printed equipment tags (PDF text)")
    every_pic = dict(every_pdf, id="p", confidence=0.75, _px=True,
                     method="printed equipment tags (piping reader, the picture's OCR layer)")
    st = {"status": "done", "rows": [gap, every_pdf, every_pic], "job": "j", "file": "upload/x/a.pdf", "page": 0}
    items = {it["id"]: it for it in C.build(st, None)["items"]}
    assert items["g"]["state"] == "check" and items["g"]["why"][0].startswith("no PS-2 on the sheet between PS-1 and PS-5")
    assert items["e"]["state"] == "ok"
    assert items["p"]["state"] == "check" and items["p"]["why"][0] == C.EVERY_LABEL_OCR and "note" not in items["p"]


def test_a_row_with_no_reading_does_not_blame_the_scale():
    row = {"value": None, "unit": "m", "confidence": 0.0, "evidence": [], "status": "ok"}
    tmap = {"tiles": [], "scale": {"status": "unknown"}, "summary": {}}
    sc = C.score(row, "What is the skirting length of the Store?", tmap, 0, used_scale=200)
    assert sc["flag"] and sc["why"][0].startswith("no reading")
    assert not any(w.startswith(("measured at 1:", "the scale is")) for w in sc["why"])
