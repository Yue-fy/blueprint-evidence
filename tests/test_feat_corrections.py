"""F7 learn from corrections: the memory store and the before()/after() hooks (no solver runs here)."""

from __future__ import annotations

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
        C.add(A, "symbol", {"label": "door", "bbox": [1, 1, 2, 2], "is": False, "bboxes": [[1, 1, 2, 2]]})


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
