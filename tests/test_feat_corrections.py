"""F7 learn from corrections: the memory store and the before()/after() hooks (no solver runs here)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stoa.features import corrections as C


@pytest.fixture()
def up(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / "abcdef012345").mkdir(parents=True)
    for n in ("a.pdf", "b.pdf", "c.png"):
        (root / "abcdef012345" / n).write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("STOA_UPLOADS", str(root))
    monkeypatch.setenv("STOA_CORRECTIONS_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("STOA_FEATURES", "corrections")
    printed = {}
    monkeypatch.setattr(C, "_printed_scale", lambda fid: printed.get(fid))
    C._CACHE.clear()
    C._RAW.clear()
    return root, printed


A, B, PNG = "upload/abcdef012345/a.pdf", "upload/abcdef012345/b.pdf", "upload/abcdef012345/c.png"


def test_store_lives_in_the_upload_folder_and_survives_reload(up):
    root, _ = up
    it = C.add(A, "scale", {"ratio": "1:200"})
    assert C.store_path(C.project_of(A)) == root / "abcdef012345" / ".stoa_corrections.json"
    C._CACHE.clear()
    got = C.items_for(B)  # same set, other sheet
    assert [i["id"] for i in got] == [it["id"]] and got[0]["ratio"] == 200
    assert C.file_id(str(root / "abcdef012345" / "a.pdf")) == A


def test_scale_applies_to_this_sheet_and_unscaled_siblings_only(up):
    _, printed = up
    C.add(A, "scale", {"ratio": 200})
    q, args, ctx = C.before(A, "What is the area of the hall?", {"gemini": 0})
    assert args["scale"] == "1:200" and args["gemini"] == 0 and ctx["used"][0]["kind"] == "scale"
    q, args, _ = C.before(B, "How long is the corridor?", {})
    assert args["scale"] == "1:200"  # sibling prints no scale: the set's scale
    printed[B] = "1:100"
    q, args, _ = C.before(B, "How long is the corridor?", {})
    assert "scale" not in args  # sibling prints its own scale: not overridden
    q, args, _ = C.before(A, "How many doors?", {})
    assert "scale" not in args  # counts do not depend on the scale
    q, args, _ = C.before(A, "Area of the hall?", {"scale": "1:50"})
    assert args["scale"] == "1:50"  # the request's own scale wins


def test_pixel_scale_and_units_per_m(up):
    C.add(PNG, "scale", {"units_per_m": 41.5, "space": "image_px"})
    _, args, _ = C.before(PNG, "area of the kitchen", {})
    assert args["px_per_m"] == 41.5 and "scale" not in args
    _, args, _ = C.before(A, "area of the kitchen", {})
    assert "px_per_m" not in args  # a picture's pixel scale is that picture's only
    it = C.add(A, "scale", {"units_per_m": C.PT_PER_M / 200, "space": "pdf_pt"})
    assert it["ratio"] == 200


def test_after_labels_a_changed_scale_answer(up):
    C.add(A, "scale", {"ratio": 200})
    q, args, ctx = C.before(A, "What is the area of the hall?", {})
    out = {"value": 244.0, "unit": "m2", "assumptions": ["scale set by the user: 1:200 (the reading used 1:100; ...)"],
           "trace": ["user scale 1:200 applied (reading used 1:100)"], "evidence": []}
    out = C.after(A, q, out, ctx)
    assert out["learned"][0]["changed"] and out["assumptions"][0].startswith("Improved using your correction")
    assert C.items_for(A)[0]["applied"] == 1


def test_off_by_default_and_learn_arg_never_reaches_the_solver(up, monkeypatch):
    monkeypatch.setenv("STOA_FEATURES", "")
    C.add(A, "scale", {"ratio": 200})
    q, args, ctx = C.before(A, "area of the hall", {"learn": 3})
    assert args == {"scale": "1:200"} and ctx["on"]
    q, args, ctx = C.before(A, "area of the hall", {"x": 1})
    assert args == {"x": 1} and not ctx["on"]
    out = {"value": 1}
    assert C.after(A, q, out, ctx) is out and "learned" not in out


def test_symbol_corrections_change_later_counts(up):
    C.add(A, "symbol", {"label": "door", "bbox": [100, 100, 110, 110], "bbox_space": "pdf_pt", "page": 0, "is": True})
    C.add(A, "symbol", {"label": "door", "bbox": [0, 0, 10, 10], "bbox_space": "pdf_pt", "page": 0, "is": False})
    ev = [{"file": A, "page": 0, "bbox": [x, x, x + 10, x + 10], "bbox_space": "pdf_pt", "note": f"door {i}"}
          for i, x in enumerate((0, 20, 40, 60, 80, 120))]
    q, args, ctx = C.before(A, "How many doors are there?", {})
    out = C.after(A, q, {"value": 6, "unit": "count", "evidence": ev}, ctx)
    assert out["value"] == 6  # +1 marked, −1 rejected
    notes = [e["note"] for e in out["evidence"]]
    assert "door 0" not in notes and "door — marked by you" in notes
    q, args, ctx = C.before(A, "How many windows are there?", {})
    assert C.after(A, q, {"value": 6, "unit": "count", "evidence": ev}, ctx)["value"] == 6  # other kind: untouched
    q, args, ctx = C.before(A, "How many double doors are there?", {})
    assert C.after(A, q, {"value": 2, "unit": "count", "evidence": ev[:2]}, ctx)["value"] == 1  # rejected box still goes


def test_choice_answers_the_same_clarification_and_aliases_later_questions(up):
    C.add(A, "choice", {"ask": "Which classroom do you mean?", "label": "Secondary Classroom 3", "phrase": "classroom"})
    cl = {"ask": "Which classroom do you mean?", "options": [
        {"label": "Secondary Classroom 1", "value": 48.0, "unit": "m2", "evidence": [0]},
        {"label": "Secondary Classroom 3", "value": 52.3, "unit": "m2", "evidence": [1]}]}
    q, args, ctx = C.before(A, "How big is the classroom?", {})
    assert q == "How big is the Secondary Classroom 3?"
    out = C.after(A, "How big is the classroom?", {"value": 48.0, "unit": "m2", "evidence": [{"note": "c1"}, {"note": "c3"}],
                                                   "clarify": cl}, ctx)
    assert out["value"] == 52.3 and out["clarify"] is None and out["evidence"] == [{"note": "c3"}]
    assert out["clarify_resolved"]["label"] == "Secondary Classroom 3"
    q, _, _ = C.before(A, "Skirting length of the Secondary Classroom 3?", {})
    assert q == "Skirting length of the Secondary Classroom 3?"  # already named: unchanged


def test_value_correction_undo_redo(up):
    it = C.add(A, "value", {"question": "How many doors are there?", "value": 13, "unit": "count"})
    q, args, ctx = C.before(A, "how many doors are there", {})
    out = C.after(A, q, {"value": 12, "unit": "count", "confidence": 0.4}, ctx)
    assert out["value"] == 13 and out["confidence"] >= 0.95 and "we read 12" in out["learned"][0]["text"]
    C.set_active(A, it["id"], False)
    q, args, ctx = C.before(A, "how many doors are there", {})
    assert C.after(A, q, {"value": 12}, ctx)["value"] == 12
    C.set_active(A, it["id"], True)
    q, args, ctx = C.before(A, "how many doors are there", {})
    assert C.after(A, q, {"value": 12}, ctx)["value"] == 13
    it2 = C.add(A, "value", {"question": "How many doors are there?", "value": 14})
    act = [i for i in C.items_for(A) if i["kind"] == "value"]
    assert [i["id"] for i in act] == [it2["id"]]  # the newer one replaced it (the old one is in the undo list)
    assert C.remove(A, it["id"]) and not C.remove(A, "nope")


def test_hooks_fail_closed(up, monkeypatch):
    monkeypatch.setattr(C, "items_for", lambda *a, **k: 1 / 0)
    q, args, ctx = C.before(A, "area of hall", {"learn": 1, "a": 2})
    assert q == "area of hall" and args == {"a": 2} and "error" in ctx


def test_router_add_list_undo(up):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(C.router)
    c = TestClient(app)
    r = c.post("/feat/corrections", json={"path": A, "kind": "symbol", "label": "door", "bbox": [1, 2, 3, 4], "is": False})
    assert r.status_code == 200 and r.json()["item"]["is"] is False
    iid = r.json()["item"]["id"]
    assert c.get("/feat/corrections", params={"path": B}).json()["items"][0]["id"] == iid
    assert c.post(f"/feat/corrections/{iid}/undo", params={"path": A}).json()["items"][0]["active"] is False
    assert c.post("/feat/corrections", json={"path": A, "kind": "scale"}).status_code == 400
    assert c.post("/feat/corrections", json={"path": A, "kind": "bogus"}).status_code == 400
    # the plugin's 'traced' flag (an accepted Suggest outline) reaches the item
    r = c.post("/feat/corrections", json={"path": A, "kind": "value", "question": "Area of the hall?", "value": 58,
                                          "unit": "m2", "source": "measure", "traced": True})
    assert r.status_code == 200 and r.json()["item"]["traced"] is True


def test_wrap_upload_reads_a_no_scale_sheet_through_a_copy_that_prints_the_users_scale(up):
    import pymupdf

    root, _ = up
    pdf = root / "abcdef012345" / "a.pdf"
    doc = pymupdf.open()
    doc.new_page(width=600, height=400).insert_text((50, 50), "Kitchen   Laundry   Multi-Purpose Hall")
    doc.save(pdf)
    C.add(A, "scale", {"ratio": 200})
    seen = {}

    def fake(req):
        seen.update(req)
        txt = pymupdf.open(req["abs"])[0].get_text()
        return {"value": 35.6, "unit": "m2", "assumptions": ["scale: 1:200 printed on the sheet; traced"],
                "trace": [], "evidence": [{"file": req["virtual"], "page": 0, "bbox": [1, 2, 3, 4], "note": "Kitchen"}],
                "_txt": txt}

    out = C.wrap_upload(fake, {"abs": str(pdf), "virtual": A, "question": "Area of the kitchen?", "solver_args": {"learn": 2}})
    assert seen["abs"] != str(pdf) and Path(seen["abs"]).name.startswith(".a.f7-1to200-")  # hidden copy, same folder
    assert seen["virtual"] == A and "learn" not in seen["solver_args"] and "scale" not in seen["solver_args"]
    assert "SCALE 1:200" in out["_txt"]
    assert out["assumptions"][1] == "scale: 1:200 set by you (the drawing prints no scale); traced"
    assert out["learned"][0]["changed"] and out["evidence"][0]["file"] == A
    # off: untouched request
    seen.clear()
    C.wrap_upload(fake, {"abs": str(pdf), "virtual": A, "question": "Area of the kitchen?", "solver_args": {"learn": 0}})
    assert seen["abs"] == str(pdf) and seen["solver_args"] == {}


def test_value_correction_finds_reworded_questions_about_the_same_room(up):
    C.add(A, "value", {"question": "What is the floor area of the Kitchen?", "value": 35.6, "unit": "m2"})
    for q, want in (("How big is the Kitchen?", 35.6), ("Area of the kitchen in m2?", 35.6),
                    ("How long is the skirting of the Kitchen?", 9.0), ("Total area of the Kitchen and Laundry?", 41.0),
                    ("What is the floor area of the Laundry?", 7.0)):
        q2, args, ctx = C.before(A, q, {})
        assert C.after(A, q2, {"value": want if want != 35.6 else 48.7, "unit": "m2"}, ctx)["value"] == want, q
    assert C._subject_of("How many metres of skirting does the Multi-Purpose Hall need?") == "Multi-Purpose Hall"
    # a trailing 'Room' is part of the name (AFCEC A-101 showed 'for the Break'; 'the Store' must not match 'Store Room')
    assert C._subject_of("What is the floor area of the Break Room in square feet?") == "Break Room"
    assert not C._about_room("Store Room", "How big is the Store?", "area")
    # US sheets say 'wall base' for skirting
    q = "How many linear feet of wall base do we need for the Break Room?"
    assert C._task_of(q) == "length" and C._length_kind(q) == C._length_kind("How long is the skirting of the Break Room?")
    assert C._task_of("How much wall base does the Break Room need?") == "length"


def test_wrap_upload_fails_closed_when_our_own_code_breaks(up, monkeypatch):
    monkeypatch.setattr(C, "before", lambda *a, **k: 1 / 0)
    seen = {}
    out = C.wrap_upload(lambda r: seen.update(r) or {"value": 1}, {"abs": "/x.pdf", "virtual": A, "question": "q",
                                                                  "solver_args": {"learn": 3, "gemini": 0}})
    assert out == {"value": 1} and seen["solver_args"] == {"gemini": 0}


def test_pixel_scale_label_credits_measured_answers_only(up):
    C.add(PNG, "scale", {"px_per_m": 59.0})
    q, args, ctx = C.before(PNG, "What is the floor area of the Laundry?", {})
    out = C.after(PNG, q, {"value": 6.94, "unit": "m2", "solver": "pipeline/agent", "evidence": []}, ctx)
    assert out["learned"][0]["changed"] and "measured at it" in out["learned"][0]["text"]
    q, args, ctx = C.before(PNG, "What is the floor area of the Laundry?", {})
    out = C.after(PNG, q, {"value": 7.0, "unit": "m2", "solver": "pipeline/area_tags",
                           "evidence": [{"note": "printed '7.0 m2'"}]}, ctx)
    assert not out["learned"][0]["changed"]


def test_no_stamp_on_a_sheet_without_a_text_layer(up):
    import pymupdf

    root, _ = up
    pdf = root / "abcdef012345" / "b.pdf"
    doc = pymupdf.open()
    doc.new_page(width=600, height=400).draw_line((10, 10), (300, 10))
    doc.save(pdf)
    C.add(B, "scale", {"ratio": 100})
    q, args, ctx = C.before(B, "Area of the kitchen?", {})
    assert "abs" not in ctx and args["scale"] == "1:100"  # outlined text: the post-hoc rescale instead


def test_unstamp_says_the_scale_is_the_users_everywhere_the_ui_reads():
    out = {"value": 57.9, "unit": "m2", "evidence": [{"file": "/x/.a.f7-1to200-k.pdf", "note": "traced at 1:200 printed on the sheet"}],
           "assumptions": ["scale: 1:200 printed on the sheet; traced"],
           "trace": ['recognised: {"scale": "1:200", "scale_source": "printed", "kind": "cad_sheet"}',
                     "[plan_rooms] lineplan: line-only sheet at printed 1:200; the room"],
           "recognition": {"scale": "1:200", "scale_source": "printed"}}
    x = C._unstamp({"ratio": 200, "text": "your scale 1:200"}, out, {"abs": "/x/.a.f7-1to200-k.pdf", "file": A})
    assert x["changed"] and out["recognition"]["scale_source"] == "user" and out["evidence"][0]["file"] == A
    assert '"scale_source": "user"' in out["trace"][0] and '"scale": "1:200"' in out["trace"][0]
    assert out["trace"][1] == "[plan_rooms] lineplan: line-only sheet at 1:200 set by you (the drawing prints no scale); the room"
    import re as _re
    text = "\n".join(out["trace"] + out["assumptions"] + [e["note"] for e in out["evidence"]])
    assert not _re.search(r"(?i)printed (?:scale|on the sheet)|scale[^\n]{0,12}printed", text)  # W5's 'printed' test


def test_a_hand_measurement_persists_with_its_outline_and_answers_later_questions(up):
    poly = [[10, 10], [60, 10], [60, 40], [10, 40]]
    C.add(A, "value", {"question": "What is the floor area of the Kitchen?", "value": 34.6, "unit": "m2", "source": "measure",
                       "subject": "Kitchen", "task": "area",
                       "evidence": [{"page": 0, "bbox": [10, 10, 60, 40], "bbox_space": "pdf_pt", "polygon": poly}]})
    C._CACHE.clear()  # a revisit: read back from disk
    q, args, ctx = C.before(A, "How big is the kitchen?", {})
    out = C.after(A, q, {"value": 48.68, "unit": "m2", "evidence": [{"file": A, "note": "Kitchen traced"}]}, ctx)
    assert out["value"] == 34.6 and out["evidence"][0]["polygon"] == poly and out["evidence"][0]["file"] == A
    assert out["evidence"][0]["note"] == "Kitchen: 34.6 m² measured by you" and out["evidence"][1]["note"].startswith("our reading (48.68)")
    assert out["learned"][0]["text"].startswith("you measured 34.6 m² for the Kitchen (we read 48.68)")


def test_a_view_pick_re_asks_later_clarifications_and_clears_the_trace_fallback(up):
    """VP's view chips (A-101: 'Which plan do you mean?', options carry a plain re-ask, no value): a remembered pick
    answers the next clarification through resolve(option question); the card's 'clarify:' trace fallback is renamed so
    it does not show the choice again; an empty re-ask keeps the picked plan's own box; the whole-sheet value is kept."""
    C.add(A, "choice", {"ask": "Which plan do you mean?", "label": "MODULE 02-ADMINISTRATION - FLOOR PLAN",
                        "question": "How many doors are there?", "phrase": "doors"})
    assert C.items_for(A)[0]["phrase"] is None  # 'Count the doors on this plan' must never alias 'doors'
    q2, _, ctx = C.before(A, "How many windows are there?", {"learn": 1})
    assert q2 == "How many windows are there?"
    cl = {"ask": "Which plan do you mean?", "options": [
        {"label": "MODULE 01- ENTRY VESTIBULE - FLOOR PLAN", "value": None, "evidence": [0],
         "question": "How many windows are there in Module 01 Entry Vestibule?"},
        {"label": "MODULE 02-ADMINISTRATION - FLOOR PLAN", "value": None, "evidence": [1],
         "question": "How many windows are there in Module 02 Administration?"}]}
    asked = []

    def resolve(q):
        asked.append(q)
        return {"value": 10, "unit": "count", "confidence": 0.4, "evidence": []}

    out = C.after(A, q2, {"value": 13, "unit": "count", "evidence": [{"note": "view 1"}, {"note": "view 2"}],
                          "clarify": cl, "trace": ["openings: 13", "clarify: Which plan do you mean? (…)"]}, ctx, resolve)
    assert asked == ["How many windows are there in Module 02 Administration?"]
    assert out["value"] == 10 and out["clarify"] is None
    assert not any(str(t).startswith("clarify: ") for t in out["trace"]) and any("clarify resolved" in t for t in out["trace"])
    assert out["evidence"] == [{"note": "MODULE 02-ADMINISTRATION - FLOOR PLAN (the plan you picked)"}]
    res = out["clarify_resolved"]
    assert res["was"] == 13 and res["others"] == ["MODULE 01- ENTRY VESTIBULE - FLOOR PLAN"]
    assert out["learned"][0]["kind"] == "choice" and out["learned"][0]["changed"]
    # undo -> the clarification is shown again
    C.set_active(A, C.items_for(A)[0]["id"], False)
    _, _, ctx = C.before(A, "How many windows are there?", {"learn": 2})
    out = C.after(A, q2, {"value": 13, "unit": "count", "evidence": [], "clarify": cl}, ctx, resolve)
    assert out["value"] == 13 and out["clarify"] == cl and "clarify_resolved" not in out


def test_a_room_phrase_still_aliases_but_object_words_never_do(up):
    C.add(A, "choice", {"ask": "Which room do you mean?", "label": "Kitchen 1.05", "phrase": "kitchen"})
    C.add(A, "choice", {"ask": "Which one do you mean?", "label": "Plan A", "phrase": "whole plan"})
    q2, _, _ = C.before(A, "How big is the kitchen on the whole plan?", {"learn": 1})
    assert q2 == "How big is the Kitchen 1.05 on the whole plan?"


def test_a_pick_between_two_readings_stays_with_its_room(up):
    """G1's 'Two readings disagree: which area do you mean?' options are named after the READER ('our area_tags
    reading'), not the room: picking it for the Kitchen must not answer the Hall's disagreement, but the Kitchen asked
    again (same words or reworded) gets the same pick; a scope pick (a plan) carries over."""
    ask = "Two readings disagree: which area do you mean?"
    C.add(A, "choice", {"ask": ask, "why": "two readings disagree", "label": "our area_tags reading", "phrase": "Kitchen",
                        "question": "What is the area of the Kitchen?", "asked": "What is the area of the Kitchen?"})
    it = C.items_for(A)[0]
    assert it["pick"] == "reading" and it["subject"] == "Kitchen" and it["task"] == "area"
    assert it["phrase"] is None  # never 'How big is the our area_tags reading?'
    assert C.before(A, "How big is the Kitchen?", {"learn": 1})[0] == "How big is the Kitchen?"

    def cl(v1, v2, q):
        return {"ask": ask, "options": [{"label": "6 x 8 m (printed dimensions)", "value": v1, "unit": "m2", "evidence": [0], "question": q},
                                        {"label": "our area_tags reading", "value": v2, "unit": "m2", "evidence": [1], "question": q}]}

    def ask_(q, v1, v2):
        q2, _, ctx = C.before(A, q, {"learn": 1})
        return C.after(A, q2, {"value": v1, "unit": "m2", "evidence": [{"note": "dims"}, {"note": "tag"}], "clarify": cl(v1, v2, q)}, ctx)

    hall = ask_("What is the area of the Hall?", 244.0, 230.0)
    assert hall["value"] == 244.0 and hall["clarify"] is not None  # another room: still asks
    again = ask_("How big is the Kitchen?", 48.0, 32.05)
    assert again["value"] == 32.05 and again["clarify"] is None and again["evidence"] == [{"note": "tag"}]
    # a Hall pick does not replace the Kitchen's
    C.add(A, "choice", {"ask": ask, "why": "two readings disagree", "label": "6 x 8 m (printed dimensions)",
                        "question": "What is the area of the Hall?", "asked": "What is the area of the Hall?"})
    assert sum(1 for i in C.items_for(A) if i["kind"] == "choice") == 2
    # a plan pick (re-ask of a narrower question) is a scope pick
    C.add(A, "choice", {"ask": "Which plan do you mean?", "label": "MODULE 02", "asked": "How many doors are there?",
                        "question": "How many doors are there in Module 02?"})
    assert C.items_for(A)[-1]["pick"] == "scope"


def test_router_keeps_why_and_asked(up):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(C.router)
    r = TestClient(app).post("/feat/corrections", json={"path": A, "kind": "choice", "ask": "Two readings disagree: which area?",
                                                         "why": "two readings disagree", "label": "x", "asked": "Area of the Kitchen?",
                                                         "question": "Area of the Kitchen?"})
    assert r.status_code == 200 and r.json()["item"]["pick"] == "reading" and r.json()["item"]["asked"] == "Area of the Kitchen?"


def test_a_marked_door_is_drawn_at_the_readers_door_size():
    same = [{"bbox": [0, 0, 30, 40]}, {"bbox": [100, 0, 132, 44]}, {"bbox": [200, 0, 228, 38]}]
    assert C._sized([88, 88, 112, 112], same) == [85.0, 80.0, 115.0, 120.0]  # median 30 x 40 around the click centre
    assert C._sized([88, 88, 112, 112], same[:1]) == [88, 88, 112, 112]  # one box: not enough to know the size
    assert C._sized([88, 88, 112, 112], [{"bbox": [0, 0, 900, 900]}] * 3) == [88, 88, 112, 112]  # a view box: keep


def test_a_measurement_that_agrees_confirms_and_one_that_differs_is_not_counted_as_two_rooms(up):
    poly = [[10, 10], [60, 10], [60, 50], [10, 50]]
    ev = [{"file": A, "page": 0, "bbox": [10, 10, 60, 50], "bbox_space": "pdf_pt", "polygon": poly}]
    C.add(A, "value", {"question": "What is the floor area of the Kitchen?", "value": 35.61, "unit": "m2",
                       "source": "measure", "task": "area", "subject": "Kitchen", "evidence": ev})
    ours = [{"file": A, "page": 0, "bbox": [9, 9, 61, 55], "note": "Kitchen: 35.61 m² traced at 1:200"}]
    q, _, ctx = C.before(A, "How big is the Kitchen?", {"learn": 1})
    out = C.after(A, q, {"value": 35.6, "unit": "m2", "confidence": 0.6, "evidence": list(ours)}, ctx)
    assert out["evidence"] == ours and out["value"] == 35.61  # same reading: no second outline
    assert out["learned"][0]["confirmed"] and not out["learned"][0]["changed"]
    assert not any("Improved using" in x for x in out.get("assumptions") or [])
    assert C.items_for(A)[0]["applied"] == 1
    q, _, ctx = C.before(A, "How big is the Kitchen?", {"learn": 2})
    out = C.after(A, q, {"value": 48.68, "unit": "m2", "evidence": [{**ours[0], "note": "Kitchen: 48.68 m² tag"}]}, ctx)
    import re
    assert out["value"] == 35.61 and out["evidence"][0]["polygon"] == poly
    assert sum(1 for e in out["evidence"] if re.search(r"\d\s*(m²|m2)(?!\w)", e["note"])) == 1  # card: ONE kitchen
    assert "our reading (48.68)" in out["evidence"][1]["note"] and "48.68 sq m" in out["evidence"][1]["note"]


def test_task_of_reads_how_many_square_metres_as_an_area():
    assert C._task_of("Kitchen: how many square metres is it?") == "area"
    assert C._task_of("How many m2 is the hall?") == "area"
    assert C._task_of("How many doors are there?") == "count"
    assert C._task_of("How many rooms are bigger than 20 m2?") == "count"


def test_a_sheet_pick_on_a_set_keeps_the_sheets_box_when_the_re_ask_marks_nothing(up):
    """stoa.sheet.sets.sheet_choices: options carry evidence as ONE dict (the sheet's plan box), not indices."""
    C.add(A, "choice", {"ask": "Which sheet do you mean?", "label": "A-101 (Ground Floor)", "asked": "How big is the hall?",
                        "question": "How big is the hall (sheet: A-101)?"})
    assert C.items_for(A)[0]["pick"] == "scope"
    cl = {"ask": "Which sheet do you mean?", "options": [
        {"label": "A-101 (Ground Floor)", "value": None, "question": "How many doors are on the ground floor (sheet: A-101)?",
         "evidence": {"file": A, "page": 0, "bbox": [1, 2, 3, 4], "bbox_space": "pdf_pt", "note": "sheet: A-101"}},
        {"label": "A-102 (Ground Floor, furniture)", "value": None, "question": "… (sheet: A-102)?",
         "evidence": {"file": B, "page": 0, "bbox": [1, 2, 3, 4], "bbox_space": "pdf_pt", "note": "sheet: A-102"}}]}
    q, _, ctx = C.before(A, "How many doors are on the ground floor?", {"learn": 1})
    out = C.after(A, q, {"value": 30, "unit": "count", "evidence": [], "clarify": cl}, ctx,
                  lambda q2: {"value": 14, "unit": "count", "evidence": []} if "A-101" in q2 else None)
    assert out["value"] == 14 and out["evidence"][0]["bbox"] == [1, 2, 3, 4] and "sheet you picked" in out["evidence"][0]["note"]


def test_read_as_is_remembered_for_the_drawing_and_applied_by_wrap_upload(up):
    """'Change' -> 'Read it as a scanned plan' on the recognition chip: later questions on that drawing go to that
    solver (when the request names none), say so, and a sheet-name correction is not replaced by it."""
    root, _ = up
    C.add(A, "sheet", {"title": "Ground floor"})
    C.add(A, "sheet", {"read_as": "raster", "label": "Scanned plan"})
    assert sum(1 for i in C.items_for(A) if i["kind"] == "sheet") == 2  # both active
    seen = []

    def answer_fn(req):
        seen.append(req.get("solver"))
        return {"value": 3, "unit": "count", "evidence": [], "trace": []}

    req = {"abs": str(root / "abcdef012345" / "a.pdf"), "virtual": A, "question": "How many doors?",
           "solver_args": {"learn": 1}}
    out = C.wrap_upload(answer_fn, req)
    assert seen == ["raster"] and out["learned"][0]["text"].startswith("read as Scanned plan")
    # the browser sends solver 'pipeline' whenever a plug-in adds ask args (web/src/api.ts): still 'automatic'
    out = C.wrap_upload(answer_fn, {**req, "solver": "pipeline"})
    assert seen[-1] == "raster" and out["learned"][0]["text"].startswith("read as Scanned plan")
    # 'Read it as detected' re-asks before the undo is saved: the request says so itself (never reaches the solver)
    got = []
    C.wrap_upload(lambda r: got.append(r) or {"value": 3, "evidence": []},
                  {**req, "solver": "pipeline", "solver_args": {"learn": 1, "read_as_off": 1}})
    assert got[0]["solver"] == "" and "read_as_off" not in got[0]["solver_args"]  # automatic = the upload default
    C.wrap_upload(answer_fn, {**req, "solver": "pdf_vector"})  # the visitor picked another reader for this one
    assert seen[-1] == "pdf_vector"
    C.wrap_upload(answer_fn, {**req, "virtual": B})  # other sheet of the set: not this drawing's choice
    assert seen[-1] in (None, "")


def test_an_unknown_read_as_solver_is_ignored(up):
    root, _ = up
    C.add(A, "sheet", {"read_as": "no_such_solver", "label": "Nonsense"})
    seen = []
    C.wrap_upload(lambda r: seen.append(r.get("solver")) or {"value": 1, "evidence": []},
                  {"abs": str(root / "abcdef012345" / "a.pdf"), "virtual": A, "question": "q?", "solver_args": {"learn": 1}})
    assert seen == [None]


def test_more_like_it_group_counts_each_new_symbol_once_and_replaces_the_single_mark(up):
    one = C.add(A, "symbol", {"label": "door", "bbox": [88, 88, 112, 112], "bbox_space": "pdf_pt", "page": 0, "is": True})
    ev = [{"file": A, "page": 0, "bbox": [x, x, x + 10, x + 10], "bbox_space": "pdf_pt", "note": f"door {i}"}
          for i, x in enumerate((0, 20, 40, 60, 140))]
    grp = C.add(A, "symbol", {"label": "door", "bbox": [95, 95, 105, 105], "bbox_space": "pdf_pt", "page": 0, "is": True,
                               "bboxes": [[95, 95, 105, 105], [200, 200, 210, 210], [41, 41, 51, 51], [300, 10, 310, 20]]})
    items = {i["id"]: i for i in C.items_for(A, active_only=False)}
    assert not items[one["id"]]["active"] and items[grp["id"]]["active"]  # the group replaces the single mark
    q, args, ctx = C.before(A, "How many doors are there?", {})
    out = C.after(A, q, {"value": 5, "unit": "count", "evidence": ev}, ctx)
    assert out["value"] == 8  # mark + 2 look-alikes; [41, 41, ...] is door 2, already counted
    notes = [e["note"] for e in out["evidence"]]
    assert notes.count("door — like the one you marked") == 2 and "door — marked by you" in notes
    assert "(2 found like yours)" in out["learned"][0]["text"]
    with pytest.raises(ValueError):
        C.add(A, "symbol", {"label": "door", "bbox": [1, 1, 2, 2], "is": False, "bboxes": [["x"]]})


def _door_pdf(path: Path) -> list:
    """A page with 5 door symbols (leaf + quarter arc, 4 orientations), thick walls and a little text."""
    import math

    import pymupdf

    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=400)
    sh = pg.new_shape()
    boxes = []
    for (hx, hy), (ux, uy) in (((100, 100), (1, 1)), ((250, 100), (-1, 1)), ((400, 100), (1, -1)), ((100, 300), (-1, -1)),
                               ((300, 300), (1, 1))):
        r = 24
        sh.draw_line((hx, hy), (hx, hy + uy * r))  # the leaf
        pts = [(hx + ux * r * math.sin(t * math.pi / 40), hy + uy * r * math.cos(t * math.pi / 40)) for t in range(21)]
        sh.draw_polyline(pts)  # the swing
        boxes.append([min(hx, hx + ux * r), min(hy, hy + uy * r), max(hx, hx + ux * r), max(hy, hy + uy * r)])
    sh.finish(color=(0, 0, 0), width=0.7)
    sh.draw_rect(pymupdf.Rect(20, 200, 580, 206))
    sh.finish(color=(0, 0, 0), fill=(0, 0, 0))
    sh.commit()
    pg.insert_text((450, 320), "Store", fontsize=9)
    doc.save(path)
    return boxes


def test_similar_finds_the_other_symbols_in_every_orientation_and_leaves_out_counted_ones(up):
    root, _ = up
    boxes = _door_pdf(root / "abcdef012345" / "doors.pdf")
    path = "upload/abcdef012345/doors.pdf"
    cx, cy = (boxes[0][0] + boxes[0][2]) / 2 + 3, (boxes[0][1] + boxes[0][3]) / 2 - 2  # a sloppy click
    r = C.similar(path, 0, [cx - 12, cy - 12, cx + 12, cy + 12], "pdf_pt", exclude=[boxes[4]])
    assert r["reason"] is None and r["counted"] == 1
    found = [b for b in boxes[1:4] if any(C._overlaps(b, h) for h in r["boxes"])]
    assert len(found) == 3 and len(r["boxes"]) == 3
    wall = C.similar(path, 0, [300, 197, 324, 209], "pdf_pt", exclude=[])
    assert wall["boxes"] == [] or len(wall["boxes"]) <= 2  # a wall piece: nothing much, never a flood of 'doors'


def test_similar_endpoint_never_raises(up):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(C.router)
    r = TestClient(app).post("/feat/corrections/similar", json={"path": "upload/abcdef012345/nope.pdf", "page": 0,
                                                                "bbox": [1, 1, 20, 20], "bbox_space": "pdf_pt"})
    assert r.status_code == 200 and r.json()["boxes"] == [] and r.json()["reason"]


def test_a_typical_size_guess_is_not_improved_by_the_users_scale():
    u = {"id": "s1", "kind": "scale", "text": "your scale 1:200 (set on this drawing)", "changed": True}
    guess = {"value": 55.7, "unit": "m2", "solver": "pipeline/agent", "assumptions": ["Scale 1:200 set by you (the drawing prints no scale)",
             "scale set by the user"], "missing": ["≈ 55.7 m² is a typical size for a Music Room: not measured"], "trace": []}
    x = C._scale_outcome(u, guess)
    assert x["changed"] is False and "typical-size guess" in x["text"]
    measured = {**guess, "missing": [], "confidence": 0.8, "evidence": [{"note": "Music Room outline", "bbox": [1, 1, 9, 9]}]}
    assert C._scale_outcome(u, measured)["changed"] is True
    prior = {"value": 55.7, "unit": "m2", "solver": "pipeline/prior", "confidence": 0.3, "assumptions": ["scale set by the user"]}
    assert C._scale_outcome(u, prior)["changed"] is False


def test_an_accepted_group_carries_to_a_sibling_sheet_only_when_both_print_the_same_scale(up):
    root, printed = up
    boxes = _door_pdf(root / "abcdef012345" / "d1.pdf")
    _door_pdf(root / "abcdef012345" / "d2.pdf")
    p1, p2 = "upload/abcdef012345/d1.pdf", "upload/abcdef012345/d2.pdf"
    cx, cy = (boxes[0][0] + boxes[0][2]) / 2, (boxes[0][1] + boxes[0][3]) / 2
    r = C.similar(p1, 0, [cx - 12, cy - 12, cx + 12, cy + 12], "pdf_pt", exclude=[])
    C.add(p1, "symbol", {"label": "door", "bbox": r["mark"], "bbox_space": "pdf_pt", "page": 0, "is": True,
                         "bboxes": [r["mark"], *r["boxes"]]})
    q, args, ctx = C.before(p2, "How many doors are there?", {})
    assert C.after(p2, q, {"value": 0, "unit": "count", "evidence": []}, ctx)["value"] == 0  # no printed scale: no carry
    printed[p1] = printed[p2] = "1:200"
    C._CROSS.clear()
    q, args, ctx = C.before(p2, "How many doors are there?", {})
    out = C.after(p2, q, {"value": 0, "unit": "count", "evidence": []}, ctx)
    assert out["value"] == 5 and "like the one you marked on d1.pdf" in out["learned"][0]["text"]
    assert all(e["note"] == "door — like the one you marked on d1.pdf" for e in out["evidence"])
    assert C._sheet_name("upload/x/SEN-DFE-XX-00-D-X-0201-Special_School_Plan_Level_00-A-C01.pdf") == "Special School Plan Level 00"
    printed[p2] = "1:100"  # another scale: symbols drawn at another size, nothing carried
    q, args, ctx = C.before(p2, "How many doors are there?", {})
    assert C.after(p2, q, {"value": 0, "unit": "count", "evidence": []}, ctx)["value"] == 0


def _mixed_pdf(path: Path) -> tuple[list, list]:
    """_door_pdf's 5 swing doors + 3 'cupboard leaves' (a short angled leaf between two wall stubs, as on DfE L00), all
    of which a reader might count as doors. Returns (door boxes, cupboard boxes)."""
    import pymupdf

    doors = _door_pdf(path)
    doc = pymupdf.open(path)
    pg = doc[0]
    sh = pg.new_shape()
    cups = []
    for x, y, flip in ((150, 250, 1), (420, 250, -1), (500, 60, 1)):
        sh.draw_rect(pymupdf.Rect(x - 2, y, x + 4, y + 14))  # wall stubs (solid)
        sh.draw_rect(pymupdf.Rect(x + 26, y, x + 32, y + 14))
        sh.finish(color=(0, 0, 0), fill=(0, 0, 0))
        a, b = (x + 4, y + 2), (x + 26, y + 8)
        sh.draw_line(a if flip > 0 else (x + 4, y + 8), b if flip > 0 else (x + 26, y + 2))  # the leaf
        sh.draw_line((x + 4, y + 2), (x + 10, y + 5))
        sh.finish(color=(0, 0, 0), width=0.7)
        cups.append([x - 2, y, x + 32, y + 14])
    sh.commit()
    doc.saveIncr()
    return doors, cups


def test_not_a_door_finds_the_counted_look_alikes_and_the_group_takes_them_all_out(up):
    """'This is not a door' on one counted cupboard leaf: alike() returns the other counted cupboard leaves (not the
    swing doors); the accepted group replaces the single click and takes 3 off the count; Undo puts them back."""
    root, printed = up
    doors, cups = _mixed_pdf(root / "abcdef012345" / "m1.pdf")
    p1 = "upload/abcdef012345/m1.pdf"
    counted = doors + cups
    r = C.alike(p1, 0, cups[0], "pdf_pt", counted)
    assert r["reason"] is None and r["counted"] == 8
    assert sorted(map(tuple, r["boxes"])) == sorted(map(tuple, cups[1:]))  # both other leaves, no swing door
    one = C.add(p1, "symbol", {"label": "door", "bbox": cups[0], "bbox_space": "pdf_pt", "page": 0, "is": False})
    grp = C.add(p1, "symbol", {"label": "door", "bbox": cups[0], "bbox_space": "pdf_pt", "page": 0, "is": False,
                               "bboxes": [cups[0], *r["boxes"]]})
    items = {i["id"]: i for i in C.items_for(p1, active_only=False)}
    assert not items[one["id"]]["active"] and items[grp["id"]]["active"]
    ev = [{"file": p1, "page": 0, "bbox": b, "bbox_space": "pdf_pt", "note": "door"} for b in counted]
    q, args, ctx = C.before(p1, "How many doors are there?", {})
    out = C.after(p1, q, {"value": 8, "unit": "count", "evidence": list(ev), "trace": ["openings: 8 doors (9 leaves)"],
                                      "assumptions": ["Counted door SETS (a double door = 1) on the sheet: 8 doors, 9 leaves"]}, ctx)
    assert out["value"] == 5 and len(out["evidence"]) == 5
    assert out["trace"][0] == "openings: 5 doors (6 leaves)"  # the card's Scope line reads the corrected count
    assert out["assumptions"][-1].endswith(": 5 doors, 6 leaves")
    assert "−3 you said are not a door (2 found like yours) (we counted 8)" in out["learned"][0]["text"]
    # a sibling sheet printing the same scale: the rejected look carries over (cut from m1, matched among m2's boxes)
    doors2, cups2 = _mixed_pdf(root / "abcdef012345" / "m2.pdf")
    p2 = "upload/abcdef012345/m2.pdf"
    ev2 = [{"file": p2, "page": 0, "bbox": b, "bbox_space": "pdf_pt", "note": "door"} for b in doors2 + cups2]
    q, args, ctx = C.before(p2, "How many doors are there?", {})
    assert C.after(p2, q, {"value": 8, "unit": "count", "evidence": list(ev2)}, ctx)["value"] == 8  # no printed scale
    printed[p1] = printed[p2] = "1:200"
    q, args, ctx = C.before(p2, "How many doors are there?", {})
    out2 = C.after(p2, q, {"value": 8, "unit": "count", "evidence": list(ev2)}, ctx)
    assert out2["value"] == 5 and "like the one you said is not a door on m1.pdf" in out2["learned"][0]["text"]
    C.set_active(p1, grp["id"], False)  # Undo: all back
    q, args, ctx = C.before(p1, "How many doors are there?", {})
    assert C.after(p1, q, {"value": 8, "unit": "count", "evidence": list(ev)}, ctx)["value"] == 8


def test_not_a_door_offers_nothing_when_most_of_the_count_looks_like_it(up):
    """One rejected window / plain door among many of the same symbol: taking them all out by look is not safe."""
    root, _ = up
    doors, cups = _mixed_pdf(root / "abcdef012345" / "m4.pdf")
    p = "upload/abcdef012345/m4.pdf"
    among = doors + [[v + 0.01 for v in b] for b in doors] + cups  # 13 counted, 9 others look like door 0
    r = C.alike(p, 0, doors[0], "pdf_pt", among)
    assert r["boxes"] == [] and "too common" in r["reason"]
    assert len(C.alike(p, 0, cups[0], "pdf_pt", among)["boxes"]) == 2  # a rare symbol still works


def test_not_a_door_look_alikes_work_on_a_picture_too(up):
    """alike() only scores the answer's own boxes (no flood possible), so it also runs on an uploaded picture."""
    import pymupdf

    root, _ = up
    doors, cups = _mixed_pdf(root / "abcdef012345" / "m3.pdf")
    k = 200 / 72
    with pymupdf.open(root / "abcdef012345" / "m3.pdf") as d:
        d[0].get_pixmap(dpi=200).save(root / "abcdef012345" / "m3.png")
    px = [[round(v * k, 1) for v in b] for b in doors + cups]
    r = C.alike("upload/abcdef012345/m3.png", 0, px[5], "image_px", px)
    assert r["reason"] is None and sorted(map(tuple, r["boxes"])) == sorted(map(tuple, px[6:]))


def test_corrections_belong_to_the_visitor_who_made_them_and_need_the_requests_opt_in(up):
    # QA 21:43 / 21:47: uploads are keyed by content, so every judge dropping the same files shares the project folder
    C.add(A, "value", {"question": "How many doors are there?", "value": 12, "unit": "count", "visitor": "judge-1"})
    seen = []

    def solve(req):
        seen.append(dict(req.get("solver_args") or {}))
        return {"value": 9, "unit": "count", "evidence": [], "assumptions": []}

    req = {"abs": "/x/a.pdf", "virtual": A, "question": "How many doors are there?"}
    # web flag off (no learn) with the API feature on: nothing applied, nothing mentioned, no switch reaches the solver
    out = C.wrap_upload(solve, {**req, "solver_args": {"visitor": "judge-1"}})
    assert out["value"] == 9 and not out.get("learned") and "visitor" not in seen[-1]
    # another judge (or an old client without an id): judge 1's correction is not theirs
    assert C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "judge-2"}})["value"] == 9
    assert C.wrap_upload(solve, {**req, "solver_args": {"learn": 1}})["value"] == 9
    # the judge who made it
    out = C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "judge-1"}})
    assert out["value"] == 12 and out["learned"] and "visitor" not in seen[-1] and "learn" not in seen[-1]
    assert [i["visitor"] for i in C.items_for(A, visitor="judge-1")] == ["judge-1"] and C.items_for(A, visitor="judge-2") == []
    # a second judge's own correction does not replace the first one's
    C.add(A, "value", {"question": "How many doors are there?", "value": 7, "unit": "count", "visitor": "judge-2"})
    assert C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "judge-1"}})["value"] == 12
    assert C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "judge-2"}})["value"] == 7


def test_room_phrase_is_the_visitors_word_for_a_room_never_the_measured_thing():
    assert C._room_phrase("What is the area of the gym?") == "gym"
    assert C._room_phrase("How big is the nurse's room?") == "nurse's room"
    assert C._room_phrase("How long is the skirting of the staff room?") == "staff room"
    assert C._room_phrase("How many doors does the gym have?") == "gym"
    assert C._room_phrase("What is the floor area of the Multi-Purpose Hall?") == "Multi-Purpose Hall"
    assert C._room_phrase("What is the area of the hall on the ground floor?") == "hall"
    for q in ("How many doors are there?", "What is the area of this plan?", "What is the total area of the building?",
              "How long is the skirting on the ground floor?"):
        assert C._room_phrase(q) is None, q


def test_the_gym_is_here_asks_later_questions_about_the_room_at_the_point(up, monkeypatch):
    # nothing printed at the point (a scan): every question about the room is read at the point
    monkeypatch.setattr(C, "_room_hint", lambda fid, page, pt, space: {"outline_bbox": [713, 683, 967, 880]})
    with pytest.raises(ValueError):
        C.add(A, "room", {"phrase": "gym"})  # no point
    with pytest.raises(ValueError):
        C.add(A, "room", {"phrase": "doors", "point": [1, 2]})  # the counted thing is no room name
    it = C.add(A, "room", {"question": "What is the area of the gym?", "point": [840, 781], "space": "pdf_pt", "page": 0,
                           "visitor": "v1"})
    assert it["phrase"] == "gym" and not it.get("label") and it["point"] == [840.0, 781.0]
    sa = {"learn": 1, "visitor": "v1"}
    for asked, want in (("How big is the gym?", "How big is this room?"),
                        ("How long is the skirting (baseboard) of the gym, door openings excluded?",
                         "How long is the skirting (baseboard) of this room, door openings excluded?"),
                        ("How many doors does the Gym have?", "How many doors does this room have?")):
        q, args, ctx = C.before(A, asked, dict(sa), page=0)
        assert q == want and ctx["context"] == {"point": [840.0, 781.0], "space": "pdf_pt", "page": 0}
        assert "visitor" not in args and "learn" not in args
    for asked in ("How many doors are there?", "What is the area of the gymnasium?", "What is the area of the Kitchen?"):
        q, _, ctx = C.before(A, asked, dict(sa), page=0)
        assert q == asked and "context" not in ctx, asked
    assert C.before(A, "How big is the gym?", dict(sa), page=1)[0] == "How big is the gym?"  # pointed at on page 1
    assert C.before(A, "How big is the gym?", dict(sa), page=0, context={"point": [1, 1]})[0] == "How big is the gym?"
    assert C.before(A, "How big is the gym?", {"learn": 1, "visitor": "v2"}, page=0)[0] == "How big is the gym?"  # not v2's
    assert C.before(B, "How big is the gym?", dict(sa), page=0)[0] == "How big is the gym?"  # another sheet: no point there
    # several rooms: asked as typed until an answer names the room at the point; then the name is an alias
    q, _, ctx = C.before(A, "What is the total area of the gym and the Library?", dict(sa), page=0)
    assert q == "What is the total area of the gym and the Library?" and "context" not in ctx
    q, _, ctx = C.before(A, "How big is the gym?", dict(sa), page=0)
    out = C.after(A, "How big is the gym?", {"value": 246.46, "unit": "m2", "evidence": [
        {"note": "Multi-Purpose Hall: 246.46 m² traced at 1:200", "bbox": [713, 683, 967, 880], "bbox_space": "pdf_pt"}]}, ctx)
    assert out["learned"][0]["text"] == "'gym' = the room you pointed at (Multi-Purpose Hall)"
    assert out["assumptions"][0].startswith("Improved using your correction: 'gym' = the room you pointed at")
    assert set(out["learned"][0]) <= {"id", "kind", "text", "changed", "confirmed"}  # no point / hint leaks to the card
    C._CACHE.clear()
    q, _, ctx = C.before(A, "What is the total area of the gym and the Library?", dict(sa), page=0)
    assert q == "What is the total area of the Multi-Purpose Hall and the Library?" and "context" not in ctx
    # same word again = replaces the old place (one Undo)
    it2 = C.add(A, "room", {"phrase": "the gym", "point": [100, 100], "visitor": "v1"})
    rooms = [i for i in C.items_for(A, visitor="v1") if i["kind"] == "room"]
    assert [i["id"] for i in rooms] == [it2["id"]] and it2["phrase"] == "gym"


def test_wrap_upload_sends_the_remembered_point_as_the_requests_context(up, monkeypatch):
    monkeypatch.setattr(C, "_room_hint", lambda *a: {})
    C.add(A, "room", {"phrase": "gym", "point": [10, 20], "space": "pdf_pt", "visitor": "v1"})
    seen = {}
    out = C.wrap_upload(lambda r: seen.update(r) or {"value": 5, "unit": "m2", "evidence": []},
                        {"abs": "/x.pdf", "virtual": A, "question": "What is the area of the gym?", "page": 0,
                         "solver_args": {"learn": "r1", "visitor": "v1"}})
    assert seen["question"] == "What is the area of this room?" and seen["context"]["point"] == [10.0, 20.0]
    assert "learn" not in seen["solver_args"] and out["learned"][0]["kind"] == "room"
    seen.clear()  # the web flag off: nothing applied, nothing said
    out = C.wrap_upload(lambda r: seen.update(r) or {"value": 5}, {"abs": "/x.pdf", "virtual": A, "question":
                        "What is the area of the gym?", "solver_args": {"visitor": "v1"}})
    assert seen["question"] == "What is the area of the gym?" and "context" not in seen and "learned" not in out


def test_a_name_printed_once_where_they_pointed_replaces_their_word_and_a_shared_one_uses_the_point(up, monkeypatch):
    hint = {"label": "Multi-Purpose Hall", "unique": True}
    monkeypatch.setattr(C, "_room_hint", lambda *a: dict(hint))
    C.add(PNG, "room", {"phrase": "gym", "point": [7005, 6560], "space": "image_px", "visitor": "v1"})
    q, _, ctx = C.before(PNG, "How many doors does the gym have?", {"learn": 1, "visitor": "v1"}, page=0)
    assert q == "How many doors does the Multi-Purpose Hall have?" and "context" not in ctx
    assert ctx["used"][0]["text"] == "'gym' = Multi-Purpose Hall (printed where you pointed)"
    hint.update(label="Store", unique=False)  # one of several stores: the point decides
    C.add(A, "room", {"phrase": "cupboard", "point": [650, 912], "visitor": "v1"})
    q, _, ctx = C.before(A, "What is the area of the cupboard?", {"learn": 1, "visitor": "v1"}, page=0)
    assert q == "What is the area of this room?" and ctx["context"]["point"] == [650.0, 912.0]
    # askWith's own context carries only the page: still applied, the page kept
    seen = {}
    C.wrap_upload(lambda r: seen.update(r) or {"value": 1}, {"abs": "/x.pdf", "virtual": A, "question":
                  "What is the area of the cupboard?", "context": {"page": 0}, "solver_args": {"learn": "r1", "visitor": "v1"}})
    assert seen["question"] == "What is the area of this room?" and seen["context"] == {"page": 0, "point": [650.0, 912.0],
                                                                                         "space": "pdf_pt"}


def test_one_of_several_rooms_of_a_name_is_read_at_the_point_for_areas_only(up, monkeypatch):
    monkeypatch.setattr(C, "_room_hint", lambda *a: {"label": "Secondary Classroom", "unique": False})
    C.add(A, "room", {"phrase": "Secondary Classroom", "point": [400, 500], "visitor": "v1"})
    sa = {"learn": 1, "visitor": "v1"}
    q, _, ctx = C.before(A, "How big is the Secondary Classroom?", dict(sa), page=0)
    assert q == "How big is this room?" and ctx["context"]["point"] == [400.0, 500.0]
    for asked in ("How long is the skirting of the Secondary Classroom?", "How many doors does the Secondary Classroom have?"):
        q, _, ctx = C.before(A, asked, dict(sa), page=0)
        assert q == asked and "context" not in ctx and ctx["skipped"][0]["why"].startswith("several rooms"), asked


def test_the_word_for_a_room_carries_to_another_sheet_that_prints_the_same_label_once(up, monkeypatch):
    monkeypatch.setattr(C, "_room_hint", lambda *a: {"label": "Multi-Purpose Hall", "unique": True,
                                                      "label_lines": ["Multi-Purpose", "Hall"]})
    C.add(A, "room", {"phrase": "gym", "point": [840, 781], "visitor": "v1"})
    from stoa.agent import click as K

    printed = {"b.pdf": [("Kitchen", (0, 0, 1, 1)), ("Multi-Purpose", (1, 1, 2, 2)), ("Hall", (1, 2, 2, 3))],
               "c.png": [("Multi-Purpose", (1, 1, 2, 2)), ("Multi-Purpose", (5, 5, 6, 6)), ("Hall", (1, 2, 2, 3))]}
    monkeypatch.setattr(K, "lines", lambda p, page=0: printed[Path(p).name])
    C._PRINTS.clear()
    q, _, ctx = C.before(B, "What is the area of the gym?", {"learn": 1, "visitor": "v1"}, page=0)
    assert q == "What is the area of the Multi-Purpose Hall?" and "context" not in ctx
    assert ctx["used"][0]["text"].startswith("'gym' = Multi-Purpose Hall (you showed it on ")
    q, _, _ = C.before(PNG, "What is the area of the gym?", {"learn": 1, "visitor": "v1"}, page=0)
    assert q == "What is the area of the gym?"  # printed twice there: which one is not known
    assert C.before(B, "What is the area of the gym?", {"learn": 1, "visitor": "v2"}, page=0)[0].endswith("gym?")


def test_the_plugins_forced_pipeline_solver_goes_back_to_the_upload_default(up):
    seen = {}
    C.wrap_upload(lambda r: seen.update(r) or {"value": 1}, {"abs": "/x.png", "virtual": PNG, "question": "q?",
                  "solver": "pipeline", "solver_args": {"learn": "r1", "visitor": "v9"}})
    assert seen["solver"] == ""  # ask_upload: '' -> 'upload' (the pipeline behind P1's normaliser)
    seen.clear()
    C.wrap_upload(lambda r: seen.update(r) or {"value": 1}, {"abs": "/x.png", "virtual": PNG, "question": "q?",
                  "solver": "vlm_direct", "solver_args": {"learn": "r1", "visitor": "v9"}})
    assert seen["solver"] == "vlm_direct"  # a reader the visitor picked stays
    seen.clear()
    C.wrap_upload(lambda r: seen.update(r) or {"value": 1}, {"abs": "/x.png", "virtual": PNG, "question": "q?",
                  "solver": "pipeline", "solver_args": {}})
    assert seen["solver"] == "pipeline"  # off: untouched


def test_a_measured_room_outline_answers_its_later_lengths_and_leaves_other_rooms_alone(up, monkeypatch):
    # 20 x 10 m outline at 10 px per m on a picture: 200 m² kept as the answer about 'the main hall' (lower-case words)
    poly = [[100, 100], [300, 100], [300, 200], [100, 200]]
    C.add(PNG, "value", {"question": "What is the floor area of the main hall?", "value": 200.0, "unit": "m2",
                         "source": "measure", "task": "area",
                         "evidence": [{"page": 0, "bbox": [100, 100, 300, 200], "bbox_space": "image_px", "polygon": poly}]})

    def ask(q, value=10.15, unit="m"):
        q2, _args, ctx = C.before(PNG, q, {"learn": "t1"})
        return C.after(PNG, q, {"value": value, "unit": unit, "evidence": [{"file": PNG, "note": "text '10.150'"}]}, ctx)

    assert ask("How big is the main hall?", 4.78, "m2")["value"] == 200.0  # the area rewording (no capital letters)
    long_ = ask("How long is the main hall?")
    assert long_["value"] == 20.0 and long_["unit"] == "m" and long_["evidence"][0]["polygon"] == poly
    assert "long side of your outline (20.00 x 10.00 m)" in long_["learned"][0]["text"] and long_["learned"][0]["changed"]
    assert long_["evidence"][1]["note"].startswith("our reading (10.15)")
    assert ask("What is the width of the main hall?")["value"] == 10.15  # within 2 % of 10.0: confirmed, kept
    assert ask("What is the width of the main hall?", 7.97)["value"] == 10.0
    assert ask("What is the perimeter of the main hall?", 8.6)["value"] == 60.0
    assert ask("How many metres of skirting go around the walls of the main hall (do not subtract door openings)?", 8.6)["value"] == 60.0
    # skirting with the doors taken off: F1's wall test along the outline (here: it finds one 1.5 m opening)
    from stoa.contract import Answer, Evidence
    from stoa.features import region as R

    monkeypatch.setattr(R, "_outline_scale", lambda abs_path, reg, ppm=None: (1 / ppm, "your scale"))
    monkeypatch.setattr(R, "outline_measure", lambda abs_path, q, reg, user_px_per_m=None: Answer(
        value=58.5, unit="m", confidence=0.7, solver="region/outline",
        evidence=[Evidence(file=abs_path, bbox=[100, 100, 300, 200], bbox_space="image_px", note="your outline"),
                  Evidence(file=abs_path, bbox=[150, 195, 165, 205], bbox_space="image_px", note="opening 1.50 m (no skirting)")]))
    sk = ask("How long is the skirting of the main hall, door openings excluded?", 7.9)
    assert sk["value"] == 58.5 and "minus 1 door opening" in sk["learned"][0]["text"] and sk["evidence"][1]["note"].startswith("opening")
    # a reading that agrees with the outline's net skirting: both find openings alike, so only 'Best estimate' (< 0.7)
    agree = ask("How long is the skirting of the main hall, door openings excluded?", 58.7)
    assert agree["value"] == 58.7 and agree["learned"][0]["confirmed"] and 0.5 <= agree["confidence"] < 0.7
    # 'wall base' (US sheets) is skirting; F1's test is asked in its own words (it answered 'wall base' with the outline's
    # AREA on AFCEC A-101), and an area coming back is never taken as metres of skirting
    seen, ev = [], [Evidence(file="x", bbox=[100, 100, 300, 200], bbox_space="image_px", note="your outline")]

    def f1(abs_path, q, reg, user_px_per_m=None):
        seen.append(q)
        return Answer(value=58.5 if "skirting" in q else 150.0, unit="m" if "skirting" in q else "m2", confidence=0.7,
                      solver="region/outline", evidence=ev)
    monkeypatch.setattr(R, "outline_measure", f1)
    assert ask("How many metres of wall base do we need for the main hall?", 7.9)["value"] == 58.5 and "skirting" in seen[-1]
    monkeypatch.setattr(R, "outline_measure", lambda abs_path, q, reg, user_px_per_m=None: Answer(
        value=150.0, unit="m2", confidence=0.7, solver="region/outline", evidence=ev))
    assert ask("How long is the skirting of the main hall, door openings excluded?", 7.9)["value"] == 7.9
    # other rooms, several rooms, and area questions about another room: untouched
    for q in ("How long is the Library?", "How long are the main hall and the Library together?"):
        assert ask(q)["value"] == 10.15 and "learned" not in ask(q)


def test_a_kept_value_answers_its_question_without_reading_the_drawing_again(up):
    # AUDIT RECHECK-0026: the saved 29.99 m² kitchen came back only after a 46 s Gemini run of the whole pipeline
    calls = []

    def solve(req):
        calls.append(req["question"])
        return {"value": 35.77, "unit": "m2", "confidence": 0.5, "solver": "pipeline/plan_rooms",
                "evidence": [{"file": A, "page": 0, "bbox": [0, 0, 10, 10], "note": "Kitchen: 35.77 m²"}]}

    poly = [[100, 100], [400, 100], [400, 300], [100, 300]]
    C.add(A, "value", {"question": "What is the floor area of the kitchen?", "value": 29.99, "unit": "m2",
                       "source": "measure", "task": "area", "subject": "kitchen", "visitor": "v1",
                       "evidence": [{"page": 0, "bbox": [100, 100, 400, 300], "polygon": poly, "bbox_space": "image_px"}]})
    req = {"abs": "/x/a.png", "virtual": A, "solver_args": {"learn": 1, "visitor": "v1"}}
    out = C.wrap_upload(solve, {**req, "question": "How big is the kitchen?"})
    assert calls == [] and out["value"] == 29.99 and out["solver"] == "corrections/kept"
    assert out["confidence"] >= 0.95 and out["evidence"][0]["polygon"] == poly and "measured by you" in out["evidence"][0]["note"]
    assert out["learned"] and out["learned"][0]["changed"] and out["assumptions"][0].startswith("Improved using your correction")
    assert out["cost"]["api_calls"] == 0 and out["data_used"] == ["local"]
    assert "we read" not in out["learned"][0]["text"]
    # anything the kept value does not answer is read as usual: another room, a total, a count, another visitor
    for q in ("How big is the laundry?", "What is the total area of the kitchen and the laundry?", "How many doors does the kitchen have?"):
        assert C.wrap_upload(solve, {**req, "question": q})["solver"] == "pipeline/plan_rooms"
    assert C.wrap_upload(solve, {**req, "question": "How big is the kitchen?", "solver_args": {"learn": 1, "visitor": "v2"}})["value"] == 35.77
    assert len(calls) == 4
    # undone: read again
    C.set_active(A, C.items_for(A, visitor="v1")[0]["id"], False)
    assert C.wrap_upload(solve, {**req, "question": "How big is the kitchen?"})["value"] == 35.77 and len(calls) == 5


def test_our_reading_note_no_longer_reads_as_the_cards_skirting_line():
    # customer.ts reads the first 'Room: skirting X m = outline Y m minus k openings' note for its sentence
    rx = re.compile(r"^'?(.+?)'?:\s*skirting\s+([\d.,]+)\s*m\s*=\s*outline\s+([\d.,]+)\s*m\s+minus\s+(\d+)\s+openings?", re.I)
    t = C._ours_note(7.94, "Maini: skirting 7.94 m = outline 8.67 m minus 1 opening")
    assert t.startswith("our reading (7.94)") and not rx.match(t)
    assert C._ours_note(35.77, "Kitchen: 35.77 m²") == "our reading (35.77): Kitchen: 35.77 sq m"


def test_a_kept_length_answers_the_same_kind_of_length_only(up):
    calls = []

    def solve(req):
        calls.append(req["question"])
        return {"value": 9.99, "unit": "m", "confidence": 0.5, "solver": "pipeline/raster", "evidence": []}

    C.add(A, "value", {"question": "How long is the main hall?", "value": 17.97, "unit": "m", "source": "measure",
                       "task": "length", "subject": "main hall", "visitor": "v1"})
    req = {"abs": "/x/a.png", "virtual": A, "solver_args": {"learn": 1, "visitor": "v1"}}
    assert C.wrap_upload(solve, {**req, "question": "What is the length of the main hall?"})["value"] == 17.97
    assert calls == []
    for q in ("How wide is the main hall?", "What is the perimeter of the main hall?",
              "How long is the skirting of the main hall?"):
        assert C.wrap_upload(solve, {**req, "question": q})["value"] == 9.99, q
    assert len(calls) == 3
    assert [C._length_kind(q) for q in ("How long is the skirting of the hall, do not subtract doors?",
                                        "How long is the skirting of the hall?", "How tall is the hall?")] == \
        ["skirting-gross", "skirting", "height"]


def test_an_accepted_suggested_outline_is_kept_as_the_visitors_choice_not_their_measurement(up):
    # F1 01:12 / audit 00:26: Measure 'Suggest' -> 'Use it' is our trace (39.59 m², 23 % off), the visitor only accepted it
    C.add(A, "value", {"question": "What is the floor area of the kitchen?", "value": 39.59, "unit": "m2", "source": "measure",
                       "task": "area", "subject": "kitchen", "traced": True, "visitor": "v1",
                       "evidence": [{"page": 0, "bbox": [0, 0, 5, 5], "polygon": [[0, 0], [5, 0], [5, 5], [0, 5]]}]})
    out = C.wrap_upload(lambda r: {"value": 35.77, "unit": "m2"},
                        {"abs": "/x/a.png", "virtual": A, "question": "How big is the kitchen?", "solver_args": {"learn": 1, "visitor": "v1"}})
    assert out["value"] == 39.59 and out["learned"][0]["text"].startswith("you chose our traced outline: 39.59 m²")
    assert "chosen by you" in out["evidence"][0]["note"] and "measured by you" not in out["evidence"][0]["note"]
    assert out["confidence"] == 0.5  # unchecked: a best estimate, not 'very sure'


def test_a_symbol_correction_corrects_the_reading_on_screen_never_a_new_one(up):
    # UX C-0 (01:15): 16 doors (5 marked) -> 'This is not a door' on mark 3 -> the re-ask read the picture again (Gemini 17)
    # and 'minus one' gave 16 -> 'Take them out' (3 look-alikes) -> 16 again: the count went UP, then did not move
    readings = iter([16, 17, 17, 17, 9])
    calls = []
    boxes = [[i * 100, 0, i * 100 + 20, 20] for i in range(5)]

    def solve(req):
        calls.append(req["question"])
        return {"value": next(readings), "unit": "count", "confidence": 0.3, "solver": "pipeline/vlm",
                "evidence": [{"file": A, "page": 0, "bbox": list(b), "bbox_space": "image_px", "note": f"door {k + 1}"}
                             for k, b in enumerate(boxes)]}

    q = "How many doors are shown on this floor plan?"
    req = {"abs": "/x/c.png", "virtual": A, "question": q, "page": 0, "solver_args": {"learn": 1, "visitor": "v1"}}
    assert C.wrap_upload(solve, req)["value"] == 16
    C.add(A, "symbol", {"label": "door", "bbox": boxes[2], "bbox_space": "image_px", "page": 0, "is": False, "visitor": "v1"})
    C._RAW.clear()  # the re-ask lands on the OTHER upload worker (Vite runs two): the disk copy is found
    out = C.wrap_upload(solve, req)
    assert out["value"] == 15 and len(calls) == 1 and out["cost"]["api_calls"] == 0  # 16 - 1, not a fresh 17 - 1
    assert any("not read again" in t for t in out["trace"])
    C.add(A, "symbol", {"label": "door", "bbox": boxes[2], "bboxes": [boxes[2], boxes[0], boxes[1], boxes[4]],
                        "bbox_space": "image_px", "page": 0, "is": False, "visitor": "v1"})
    assert C.wrap_upload(solve, req)["value"] == 12 and len(calls) == 1  # 'Take them out': 16 - 4
    # another visitor, another question: read as usual
    assert C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "v2"}})["value"] == 17
    assert len(calls) == 2
    # another tool's own switch in the request (a scale arg), or private mode -> read again
    C.wrap_upload(solve, {**req, "solver_args": {"learn": 1, "visitor": "v1", "scale": "1:50"}})
    C.wrap_upload(solve, {**req, "private": True})
    assert len(calls) == 4
    calls[:] = calls[:2]
    # a correction that changes what the reader sees (a scale) -> read again
    C.add(A, "scale", {"ratio": 100, "visitor": "v1"})
    C.wrap_upload(solve, req)
    assert len(calls) == 3


def test_a_first_symbol_correction_corrects_the_plain_answer_the_visitor_saw(up):
    # UX 02:55: a visitor with no correction asks plainly (no learn: the pre-warmed answers), so no reading of that answer
    # was kept; the plug-in sends it once with the re-ask (solver_args.shown) and THAT reading is corrected
    calls = []
    boxes = [[i * 100, 0, i * 100 + 20, 20] for i in range(5)]

    def solve(req):
        calls.append(dict(req.get("solver_args") or {}))
        return {"value": 17, "unit": "count", "confidence": 0.3, "solver": "pipeline/gemini", "evidence": []}

    q = "How many doors are shown on this floor plan?"
    seen = {"value": 16, "unit": "count", "confidence": 0.3, "solver": "pipeline/vlm", "trace": ["read by the local model"],
            "cached": True, "evidence": [{"file": A, "page": 0, "bbox": list(b), "bbox_space": "image_px", "note": f"door {k + 1}"}
                                         for k, b in enumerate(boxes)]}
    plain = {"abs": "/x/c.png", "virtual": A, "question": q, "page": 0}
    assert C.wrap_upload(solve, {**plain, "solver_args": {"shown": seen}})["value"] == 17  # no learn: passes through
    assert calls == [{}]  # 'shown' never reaches a reader
    C.add(A, "symbol", {"label": "door", "bbox": boxes[2], "bbox_space": "image_px", "page": 0, "is": False, "visitor": "v1"})
    req = {**plain, "solver_args": {"learn": "r1", "visitor": "v1", "shown": seen}}
    out = C.wrap_upload(solve, req)
    assert out["value"] == 15 and len(calls) == 1 and out["cost"]["api_calls"] == 0  # 16 - 1, not a fresh 17 - 1
    assert any("not read again" in t for t in out["trace"]) and "cached" not in out
    # 'Take them out' (no shown this time): the kept reading is found (other worker: the disk copy)
    C._RAW.clear()
    C.add(A, "symbol", {"label": "door", "bbox": boxes[2], "bboxes": [boxes[2], boxes[0], boxes[1], boxes[4]],
                        "bbox_space": "image_px", "page": 0, "is": False, "visitor": "v1"})
    assert C.wrap_upload(solve, {**plain, "solver_args": {"learn": "r2", "visitor": "v1"}})["value"] == 12 and len(calls) == 1
    # never in its place: an answer a correction already changed, or with a correction that changes the reading active
    assert not C._seed_ok({**seen, "learned": [{"id": "x"}]}, "")
    assert not C._seed_ok({**seen, "trace": ["kept: the visitor's own answer"]}, "")
    assert not C._seed_ok(seen, "abc123") and not C._seed_ok({"value": None}, "") and not C._seed_ok("16", "")
    C.add(PNG, "scale", {"ratio": 100, "visitor": "v3"})
    C.add(PNG, "symbol", {"label": "door", "bbox": boxes[2], "bbox_space": "image_px", "page": 0, "is": False, "visitor": "v3"})
    C.wrap_upload(solve, {**plain, "virtual": PNG, "solver_args": {"learn": "r4", "visitor": "v3", "shown": seen}})
    assert len(calls) == 2 and "shown" not in calls[-1]  # read again (the scale changes the reading)


def test_a_view_or_room_box_in_the_evidence_is_never_taken_for_a_door(up):
    # walk c10 (night's reader adds 'view: 1.krs' boxes): 'This is not a door' on door 3 took the view holding it out
    # too (16 -> 14), 'Take them out' -6 for 4 doors; a door the visitor marked inside a view was 'already counted'
    doors = [[389, 128, 407, 193], [1039, 160, 1054, 192], [503, 600, 520, 637], [244, 690, 283, 706], [335, 777, 350, 814]]
    ev = [{"file": A, "page": 0, "bbox": b, "bbox_space": "image_px", "note": f"door {k + 1}"} for k, b in enumerate(doors)]
    ev += [{"file": A, "page": 0, "bbox": [686.41, 75.2, 1170.37, 518.67], "bbox_space": "image_px", "note": "view: 2.krs"},
           {"file": A, "page": 0, "bbox": [40.49, 61.7, 605.43, 859.94], "bbox_space": "image_px", "note": "view: 1.krs"}]
    q = "How many doors are shown on this floor plan?"
    out0 = {"value": 16, "unit": "count", "evidence": ev}
    C.add(A, "symbol", {"label": "door", "bbox": doors[2], "bbox_space": "image_px", "page": 0, "is": False})
    _, _, ctx = C.before(A, q, {"learn": 1})
    out = C.after(A, q, json_copy(out0), ctx)
    assert out["value"] == 15 and sum(1 for e in out["evidence"] if str(e["note"]).startswith("view")) == 2
    C.add(A, "symbol", {"label": "door", "bbox": [100, 400, 118, 440], "bbox_space": "image_px", "page": 0, "is": True})
    _, _, ctx = C.before(A, q, {"learn": 1})
    assert C.after(A, q, json_copy(out0), ctx)["value"] == 16  # -1 (door 3) +1 (the one marked inside view 1)
    # a room's outline among a room's door marks is a region by size (no note needed)
    room = {"file": B, "page": 0, "bbox": [0, 0, 400, 300], "bbox_space": "pdf_pt", "note": "Laundry 6.97 m²"}
    d2 = [{"file": B, "page": 0, "bbox": [10, 10, 22, 22], "bbox_space": "pdf_pt", "note": "door 1"},
          {"file": B, "page": 0, "bbox": [200, 10, 214, 22], "bbox_space": "pdf_pt", "note": "door 2"}]
    assert C._region_box(room, [room, *d2]) and not C._region_box(d2[0], [room, *d2])
    assert not C._region_box(d2[0], d2[:1])


def json_copy(x):
    import json

    return json.loads(json.dumps(x))


def test_start_over_forgets_this_visitors_corrections_everywhere_but_the_uploads_they_keep(up):
    # captain 01:18 (W5's 'Start over'): catalogue cases always, own uploads only when the visitor confirms
    D = "ifc_models/demo/house.ifc"  # a catalogue / dataset drawing: memory_dir store
    C.add(A, "scale", {"ratio": 100, "visitor": "v1"})
    C.add(A, "scale", {"ratio": 50, "visitor": "v2"})
    C.add(D, "value", {"question": "How many doors?", "value": 3, "visitor": "v1"})
    assert C.forget("v1", keep=[A]) == {"removed": 1, "projects": [C.project_of(D)]}
    assert C.items_for(D, visitor="v1") == [] and C.items_for(A, visitor="v1")
    assert C.forget("v1")["removed"] == 1 and C.items_for(A, visitor="v1") == []
    assert [i["ratio"] for i in C.items_for(A, visitor="v2")] == [50]  # another visitor's are never touched
    with pytest.raises(ValueError):
        C.forget("")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(C.router)
    r = TestClient(app).post("/feat/corrections/forget", json={"visitor": "v2", "path": A})
    assert r.status_code == 200 and r.json()["removed"] == 1 and r.json()["items"] == []


def test_a_kept_value_follows_the_unit_the_question_asks_for_and_a_ft2_outline_answers_lengths_in_feet(up):
    # US sheets (Stoa's AFCEC '6,095 SF', the 1966 USDA farmhouse 'LIVING ROOM 15'-2" x 11'-6"'): the plug-in keeps a Measure
    # result in ft² after a ft² answer; later questions about that room get feet, and a question naming m² gets m²
    poly = [[100, 100], [300, 100], [300, 200], [100, 200]]  # 20 x 10 m at 10 px per m = 200 m² = 2152.78 ft²
    C.add(PNG, "value", {"question": "What is the floor area of the living room?", "value": 2152.78, "unit": "ft2",
                         "source": "measure", "task": "area", "subject": "living room",
                         "evidence": [{"page": 0, "bbox": [100, 100, 300, 200], "bbox_space": "image_px", "polygon": poly}]})

    def ask(q, value, unit):
        _q2, _args, ctx = C.before(PNG, q, {"learn": "t1"})
        return C.after(PNG, q, {"value": value, "unit": unit, "evidence": [{"file": PNG, "note": "text '15-2'"}]}, ctx)

    big = ask("How big is the living room?", 174.42, "ft2")
    assert big["value"] == 2152.78 and big["unit"] == "ft2" and "2152.78 ft²" in big["learned"][0]["text"]
    sqm = ask("How big is the living room in square metres?", 16.2, "m2")
    assert sqm["value"] == 200.0 and sqm["unit"] == "m2" and "(= your 2152.78 ft²)" in sqm["learned"][0]["text"]
    long_ = ask("How long is the living room along its longer side?", 15.17, "ft")
    assert long_["unit"] == "ft" and abs(long_["value"] - 65.62) < 0.02, long_
    assert "(65.62 x 32.81 ft)" in long_["learned"][0]["text"] and "2152.78 ft² outline" in long_["evidence"][0]["note"]
    assert ask("What is the perimeter of the living room in metres?", 30.0, "m")["value"] == 60.0
    # our reading in metres is compared with their outline in one unit: 3.05 m (= 10 ft) is not their 10 m side, 10.0 m is
    other = ask("How wide is the living room?", 3.05, "m")
    assert other["value"] == 32.81 and other["unit"] == "ft" and other["learned"][0]["changed"]
    agree = ask("How wide is the living room?", 10.0, "m")
    assert agree["value"] == 10.0 and agree["learned"][0]["confirmed"]
    # agreement is said in the reading's unit (walk c8: '… agrees (the perimeter of your outline: 54.0 ft)' under 16.53 m)
    per = ask("What is the perimeter of the living room?", 60.5, "m")
    assert per["value"] == 60.5 and per["unit"] == "m" and per["learned"][0]["text"].endswith(": 60.0 m)")
    assert per["confidence"] >= 0.75
    # a m² measurement and a question in square feet: converted, said
    C.add(A, "value", {"question": "What is the floor area of the kitchen?", "value": 29.99, "unit": "m2", "source": "measure",
                       "task": "area", "subject": "kitchen"})
    _q2, _args, ctx = C.before(A, "How many square feet is the kitchen?", {"learn": "t1"})
    sf = C.after(A, "How many square feet is the kitchen?", {"value": 385.0, "unit": "ft2", "evidence": []}, ctx)
    assert sf["value"] == 322.81 and sf["unit"] == "ft2" and "(= your 29.99 m²)" in sf["learned"][0]["text"]
    assert [C._asked_unit(q) for q in ("Area in sq ft?", "How many SF is it?", "in square meters", "How many feet long?",
                                       "How long is it?", "How many linear feet of skirting?", "How big is room SF-01?",
                                       "Is it 6,095 SF?")] == \
        ["ft2", "ft2", "m2", "ft", None, "ft", None, "ft2"]


def test_a_kept_room_value_never_answers_a_question_about_a_thing_in_that_room(up):
    # the kept answer skips the readers, so a false match is a confident wrong answer: a kept door count of the kitchen is
    # not its window count; the kitchen's kept area / outline is not the area / width of its door, window or counter
    calls = []

    def solve(req):
        calls.append(req["question"])
        return {"value": 7.0, "unit": "count" if "many" in req["question"] else "m", "confidence": 0.5,
                "solver": "pipeline/x", "evidence": []}

    req = {"abs": "/x/a.png", "virtual": A, "solver_args": {"learn": 1, "visitor": "v1"}}
    C.add(A, "value", {"question": "How many doors does the kitchen have?", "value": 3, "unit": "count", "visitor": "v1"})
    assert C.wrap_upload(solve, {**req, "question": "How many doors are there in the kitchen?"})["value"] == 3
    assert C.wrap_upload(solve, {**req, "question": "How many windows does the kitchen have?"})["value"] == 7.0
    poly = [[100, 100], [300, 100], [300, 200], [100, 200]]
    C.add(A, "value", {"question": "What is the floor area of the kitchen?", "value": 200.0, "unit": "m2", "source": "measure",
                       "task": "area", "visitor": "v1",
                       "evidence": [{"page": 0, "bbox": [100, 100, 300, 200], "bbox_space": "image_px", "polygon": poly}]})
    assert C.wrap_upload(solve, {**req, "question": "What is the area of the kitchen floor?"})["value"] == 200.0
    assert C.wrap_upload(solve, {**req, "question": "How long is the kitchen?"})["value"] == 20.0
    for q in ("How big is the kitchen window?", "What is the area of the windows in the kitchen?",
              "How wide is the kitchen door?", "How wide is the door of the kitchen?", "How long is the kitchen counter?"):
        out = C.wrap_upload(solve, {**req, "question": q})
        assert out["value"] == 7.0 and not out.get("learned"), q


def test_a_kept_storey_or_quantity_phrase_value_never_answers_another_room_or_storey(up):
    calls = []

    def solve(req):
        calls.append(req["question"])
        return {"value": 9.0, "unit": "m2", "confidence": 0.5, "solver": "pipeline/x", "evidence": []}

    req = {"abs": "/x/a.png", "virtual": A, "solver_args": {"learn": 1, "visitor": "v1"}}
    C.add(A, "value", {"question": "What is the floor area of Level 01?", "value": 1200.0, "unit": "m2", "visitor": "v1"})
    C.add(A, "value", {"question": "What is the gross floor area of the ground floor?", "value": 2398.0, "unit": "m2", "visitor": "v1"})
    assert C.wrap_upload(solve, {**req, "question": "How big is Level 01?"})["value"] == 1200.0
    assert C.wrap_upload(solve, {**req, "question": "What is the gross floor area of the ground floor?"})["value"] == 2398.0
    for q in ("What is the floor area of the kitchen on Level 01?", "What is the gross floor area of the first floor?"):
        assert C.wrap_upload(solve, {**req, "question": q})["value"] == 9.0, q


def test_a_thing_in_the_pointed_room_is_asked_as_the_thing_of_this_room(up, monkeypatch):
    monkeypatch.setattr(C, "_room_hint", lambda fid, page, pt, space: {"outline_bbox": [713, 683, 967, 880]})
    C.add(A, "room", {"question": "What is the area of the gym?", "point": [840, 781], "space": "pdf_pt", "page": 0, "visitor": "v1"})
    sa = {"learn": 1, "visitor": "v1"}
    for asked, want in (("How wide is the gym door?", "How wide is the door of this room?"),
                        ("How long is the gym's north wall?", "How long is the north wall of this room?"),
                        ("How many windows does the gym have?", "How many windows does this room have?"),
                        ("How tall is the gym?", "How tall is this room?")):
        q, _, ctx = C.before(A, asked, dict(sa), page=0)
        assert q == want and ctx["context"]["point"] == [840.0, 781.0], asked


def test_a_value_kept_on_one_page_of_a_multi_page_upload_answers_that_page_only(up):
    calls = []

    def solve(req):
        calls.append(req.get("page"))
        return {"value": 41.0, "unit": "m2", "confidence": 0.5, "solver": "pipeline/x", "evidence": []}

    C.add(A, "value", {"question": "What is the floor area of the kitchen?", "value": 29.99, "unit": "m2", "page": 0,
                       "source": "measure", "task": "area", "visitor": "v1"})
    req = {"abs": "/x/a.pdf", "virtual": A, "question": "How big is the kitchen?", "solver_args": {"learn": 1, "visitor": "v1"}}
    assert C.wrap_upload(solve, {**req, "page": 0})["value"] == 29.99
    assert C.wrap_upload(solve, {**req, "page": 1})["value"] == 41.0  # Level 01's kitchen: read, not Level 00's value
    assert C.wrap_upload(solve, req)["value"] == 29.99  # no page said: as before
    assert C.wrap_upload(solve, {**req, "context": {"page": 1}})["value"] == 41.0
    assert calls == [1]  # page 0 never read; page 1 read once (its reading on screen is reused)
