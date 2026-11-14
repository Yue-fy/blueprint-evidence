"""T2 feature 'privacy': the storage note and 'Delete my files' (upload folder + caches tied to it, nothing else)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stoa.features import privacy


@pytest.fixture()
def world(tmp_path, monkeypatch):
    up, shared = tmp_path / "uploads", tmp_path / "shared"
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(shared))
    folder = up / "0123456789ab"
    folder.mkdir(parents=True)
    (folder / "plan.png").write_bytes(b"png")
    (folder / "plan.6000.png").write_bytes(b"copy")
    other = up / "ba9876543210"
    other.mkdir()
    (other / "plan.png").write_bytes(b"another visitor")
    ocr = shared / "artifacts" / "O1" / "ocr"
    ocr.mkdir(parents=True)
    mine = ocr / "plan.abc123.json"
    mine.write_text(f'{{"path": "{folder / "plan.png"}"}}')
    theirs = ocr / "plan.def456.json"
    theirs.write_text(f'{{"path": "{other / "plan.png"}"}}')
    app = FastAPI()
    app.include_router(privacy.router)
    return {"client": TestClient(app), "folder": folder, "other": other, "mine": mine, "theirs": theirs}


def test_note_names_every_destination(world):
    r = world["client"].get("/feat/privacy/note").json()
    assert "Gemini" in r["text"] and "NVIDIA" in r["text"] and "Delete my files" in r["text"]
    assert all({"what", "where", "kept"} <= set(i) for i in r["items"])


def test_delete_removes_the_folder_and_its_own_caches_only(world):
    r = world["client"].post("/feat/privacy/delete", json={"upload": "upload/0123456789ab/plan.png"}).json()
    assert r["ok"] and not world["folder"].exists()
    assert not world["mine"].exists()
    assert world["theirs"].exists() and world["other"].exists()  # another visitor's same-named file is untouched
    assert any("llm-cache" in k["where"] for k in r["kept"])  # what it cannot trace is said, not hidden


@pytest.mark.parametrize("ref", ["../etc", "upload/../../x", "/etc/passwd", "upload/0123456789ab/../../x/y",
                                 "upload/zzzzzzzzzzzz", ""])
def test_delete_refuses_anything_but_an_upload_folder(world, ref):
    r = world["client"].post("/feat/privacy/delete", json={"upload": ref})
    assert r.status_code == 400
    assert world["folder"].exists()


def _two_page_pdf(path):
    import pymupdf

    doc = pymupdf.open()
    for i in range(2):
        pg = doc.new_page(width=842, height=595)
        pg.draw_rect(pymupdf.Rect(100, 100, 400, 300), width=2)
        pg.insert_text((150, 200), f"ROOM {i + 1}", fontsize=12)
        pg.insert_text((150, 400), "FLOOR PLAN" if i else "COVER SHEET", fontsize=14)
    doc.save(str(path))
    doc.close()


def test_delete_removes_page_copies_and_renders_made_by_their_owners(tmp_path, monkeypatch):
    """Cycle 2: the copies of the drawing named by a hash of path + size + time, made by the owners' own functions
    (so a changed key scheme fails here): the page copy of a multi-page PDF (stoa.pipeline._pick_pdf_page), the PDF
    text reader's page render (pdf_textqa.render_with_text, also of that page copy) and the door detector's render."""
    import tempfile

    from stoa import pipeline
    from stoa.solvers import door_detect
    from stoa.solvers.vector import pdf_textqa

    up, shared, tmp = tmp_path / "uploads", tmp_path / "shared", tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(shared))
    monkeypatch.delenv("STOA_PAGE_DIR", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    folder = up / "a1b2c3d4e5f6"
    folder.mkdir(parents=True)
    pdf = folder / "set.pdf"
    _two_page_pdf(pdf)
    other = up / "f6e5d4c3b2a1"
    other.mkdir()
    _two_page_pdf(other / "set.pdf")

    made = []
    for p in (pdf, other / "set.pdf"):
        picked = pipeline._pick_pdf_page(str(p), "What is the area of ROOM 2 on the floor plan?")
        assert picked is not None, "the page picker made no page copy (test sheet must be multi-page)"
        copy = picked[0]
        made.append([Path(copy), Path(pdf_textqa.render_with_text(str(p))[0]),
                     Path(pdf_textqa.render_with_text(copy)[0]), Path(door_detect._render(str(p), 72)[0])])
    mine, theirs = made
    assert all(f.exists() for f in mine + theirs)

    r = privacy.delete_upload("upload/a1b2c3d4e5f6/set.pdf")
    assert r["ok"]
    left = [str(f) for f in mine if f.exists()]
    assert left == [], f"copies of the deleted upload left behind: {left}"
    assert all(f.exists() for f in theirs), "another upload's copies were removed"
    assert any("llm-cache" in k["where"] for k in r["kept"])



def test_delete_removes_takeoffs_and_orphaned_projects(tmp_path, monkeypatch):
    up, tko, prj = tmp_path / "uploads", tmp_path / "takeoff", tmp_path / "projects"
    for d in (tko, prj):
        d.mkdir()
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tko))
    monkeypatch.setenv("STOA_PROJECTS_DIR", str(prj))
    mine, other = up / "0123456789ab", up / "ba9876543210"
    for d, body in ((mine, b"my sheet"), (other, b"their sheet")):
        d.mkdir(parents=True)
        (d / "plan.pdf").write_bytes(body)
    from stoa.features.takeoff import job_key  # the owner's own key: a changed scheme fails here

    my_job, their_job = tko / job_key(str(mine / "plan.pdf"), 0), tko / job_key(str(other / "plan.pdf"), 0)
    my_job.mkdir()
    their_job.mkdir()
    only_mine = prj / "p0123456789abcd.json"
    only_mine.write_text('{"files": ["upload/0123456789ab/plan.pdf"]}')
    shared_set = prj / "p0000000000000a.json"
    shared_set.write_text('{"files": ["upload/0123456789ab/plan.pdf", "upload/ba9876543210/plan.pdf"]}')
    r = privacy.delete_upload("upload/0123456789ab/plan.pdf")
    assert r["ok"]
    assert not my_job.exists() and their_job.exists()
    assert not only_mine.exists() and shared_set.exists()  # a project still holding another upload stays


def test_retention_is_off_by_default_and_expires_old_uploads_when_set(tmp_path, monkeypatch):
    import os
    import time

    up = tmp_path / "uploads"
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    monkeypatch.delenv("STOA_UPLOAD_TTL_HOURS", raising=False)
    old, new, kept = up / "0123456789ab", up / "111111111111", up / "222222222222"
    for d in (old, new, kept):
        d.mkdir(parents=True)
        (d / "plan.png").write_bytes(b"x")
    (kept / ".keep").write_text("")
    day_ago = time.time() - 30 * 3600
    for d in (old, kept):
        for p in (*d.iterdir(), d):
            os.utime(p, (day_ago, day_ago))
    assert privacy.expire() == [] and old.exists()  # off by default: nothing is ever expired
    assert "retention_hours" in privacy.note() and privacy.note()["retention_hours"] is None
    monkeypatch.setenv("STOA_UPLOAD_TTL_HOURS", "24")
    gone = privacy.expire()
    assert [g["upload"] for g in gone] == ["0123456789ab"] and not old.exists()
    assert new.exists() and kept.exists()
    n = privacy.note()
    assert n["retention_hours"] == 24 and "24 hours" in n["text"] and "24 hours" in n["items"][0]["kept"]


def test_room_maps_go_once_they_are_content_named(world, monkeypatch, tmp_path):
    """Before the R1 patch (experiments/t2/r1_room_map_names.patch) maps are named by file name alone: kept and said;
    with it (graph.map_name) the upload's own map is removed and another upload's same-name map stays."""
    import sys
    import types

    live = tmp_path / "shared" / "artifacts" / "R1" / "cc5k_seg_live"
    live.mkdir(parents=True)
    stem_only = live / "plan.npz"
    stem_only.write_bytes(b"whose?")
    mine = live / "plan.0123456789.npz"
    mine.write_bytes(b"mine")
    fake = types.ModuleType("stoa.extras.room_graph.graph")
    fake.map_name = lambda p: "plan.0123456789" if "0123456789ab" in p and p.endswith("plan.png") else "x.none"
    monkeypatch.setitem(sys.modules, "stoa.extras.room_graph.graph", fake)
    r = privacy.delete_upload("upload/0123456789ab/plan.png")
    assert not mine.exists() and stem_only.exists()
    assert not any("room maps" in k["why"] for k in r["kept"])


def test_room_maps_named_by_file_name_are_listed_as_kept(world, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "stoa.extras.room_graph.graph", None)  # import fails -> unpatched behaviour
    r = privacy.delete_upload("upload/0123456789ab/plan.png")
    assert any("room maps" in k["why"] for k in r["kept"])
