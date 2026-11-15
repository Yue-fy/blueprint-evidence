"""T2 feature 'privacy': the storage note and 'Delete my files' (upload folder + caches tied to it, nothing else)."""

from __future__ import annotations

import os
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


# -- cycle 3: shared uploads (catalogue examples, '.keep', the same bytes uploaded twice) ------------------------

def test_an_example_sheet_opened_as_an_upload_is_never_deleted(world, monkeypatch, tmp_path):
    """W5's catalogue (/api/case) and W6's phone page open the example sheets as uploads in a content-named folder and
    cache that in the web server's memory: deleting it would break the card for every later visitor."""
    ex = tmp_path / "examples"
    ex.mkdir()
    (ex / "plan.png").write_bytes(b"png")  # the world's plan.png = this example, same name: the catalogue's folder
    (ex / "school.png").write_bytes(b"another visitor")  # the other upload has these bytes under ANOTHER name
    monkeypatch.setenv("STOA_SAMPLE_DIRS", str(ex))
    r = world["client"].post("/feat/privacy/delete", json={"upload": "upload/0123456789ab/plan.png"}).json()
    assert r["ok"] and r["deleted"] == [] and "example sheets" in r["sample"]
    assert world["folder"].exists() and world["mine"].exists()
    r = privacy.delete_upload("upload/ba9876543210/plan.png")  # a visitor's own copy of example bytes: deleted
    assert "sample" not in r and r["deleted"] and not world["other"].exists()
    monkeypatch.setenv("STOA_SAMPLE_DIRS", "")  # no longer an example: an ordinary upload again
    (ex / "plan.png").unlink()
    assert privacy.delete_upload("0123456789ab")["deleted"]


def test_an_operator_keep_folder_is_never_deleted(world):
    (world["folder"] / ".keep").write_text("")
    r = privacy.delete_upload("upload/0123456789ab/plan.png")
    assert r["deleted"] == [] and r["sample"] and world["folder"].exists()


def test_a_takeoff_shared_with_another_upload_of_the_same_bytes_stays(tmp_path, monkeypatch):
    up, tko = tmp_path / "uploads", tmp_path / "takeoff"
    tko.mkdir()
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tko))
    mine, twin = up / "0123456789ab", up / "ba9876543210"  # the same drawing uploaded under two names
    mine.mkdir(parents=True)
    twin.mkdir()
    (mine / "plan.pdf").write_bytes(b"one drawing")
    (twin / "school.pdf").write_bytes(b"one drawing")
    from stoa.features.takeoff import job_key

    job = tko / job_key(str(mine / "plan.pdf"), 0)
    assert job == tko / job_key(str(twin / "school.pdf"), 0)  # keyed by content: one takeoff for both
    job.mkdir()
    r = privacy.delete_upload("upload/0123456789ab/plan.pdf")
    assert r["ok"] and not mine.exists() and job.exists()
    assert any("another upload" in k["why"] for k in r["kept"])
    r = privacy.delete_upload("upload/ba9876543210/school.pdf")  # the last copy goes: so does its takeoff
    assert r["ok"] and not job.exists()


def test_retention_never_expires_an_example_folder(tmp_path, monkeypatch):
    import time

    up, ex = tmp_path / "uploads", tmp_path / "examples"
    ex.mkdir()
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    monkeypatch.setenv("STOA_SAMPLE_DIRS", str(ex))
    sample, mine = up / "0123456789ab", up / "ba9876543210"
    for d, body in ((sample, b"the catalogue sheet"), (mine, b"a visitor's sheet")):
        d.mkdir(parents=True)
        (d / "plan.pdf").write_bytes(body)
    (ex / "plan.pdf").write_bytes(b"the catalogue sheet")
    gone = privacy.expire(ttl=1, now=time.time() + 7200)
    assert [g["upload"] for g in gone] == ["ba9876543210"]
    assert sample.exists() and not mine.exists()


def test_note_says_whether_upload_answers_are_measured(world, tmp_path):
    """The panel shows 'no outside connection during this answer' only on a server whose upload path measures it."""
    from stoa import netguard

    assert netguard.install()
    old, new = tmp_path / "old_ask_upload.py", tmp_path / "new_ask_upload.py"
    old.write_text("def answer(req):\n    return {}\n")
    new.write_text("from stoa import netguard as _ng\nnet = _ng.watch()\n")
    assert privacy.measures_destinations(old) is False
    assert privacy.measures_destinations(new) is True
    assert privacy.measures_destinations(tmp_path / "missing.py") is False
    assert "measured" in world["client"].get("/feat/privacy/note").json()


def test_an_image_example_is_recognised_by_its_folder_id_though_its_bytes_changed(world, monkeypatch, tmp_path):
    """saveUpload normalises an image IN PLACE (DfE L00: 1,058,913 bytes in the example folder, 538,820 in the
    upload), so the catalogue's folder holds other bytes than the example; its id is sha256(body + IMAGE_NORM_V +
    '|name:' + name)[:12]. (22:17 a real 'Delete my files' on the DfE L00 catalogue card removed that folder.)"""
    ex = tmp_path / "examples"
    ex.mkdir()
    body = b"\x89PNG original example bytes"
    (ex / "dfe-plan.png").write_bytes(body)
    monkeypatch.setenv("STOA_SAMPLE_DIRS", str(ex))
    fid = sorted(i for i in privacy.upload_ids(body, "dfe-plan.png"))[-1]
    folder = Path(privacy.uploads_root()) / fid
    folder.mkdir()
    (folder / "dfe-plan.png").write_bytes(b"\x89PNG normalised: other bytes")
    r = privacy.delete_upload(f"upload/{fid}/dfe-plan.png")
    assert r["deleted"] == [] and "example sheets" in r["sample"] and folder.exists()


def test_the_folder_id_rule_matches_the_web_servers(world):
    """Pinned to web/mock/stoaApi.ts: if saveUpload's id or IMAGE_NORM_V changes, this fails instead of the catalogue
    folders becoming deletable again."""
    src = (Path(__file__).resolve().parents[1] / "web" / "mock" / "stoaApi.ts").read_text()
    assert ('createHash("sha256").update(body).update(image ? IMAGE_NORM_V : "").update(`|name:${safe}`)'
            '.digest("hex").slice(0, 12)') in src
    assert 'replace(/[^\\w.\\- ()]+/g, "_").replace(/^\\.+/, "").slice(-120) || "upload"' in src
    assert privacy._image_norm_v() and privacy._image_norm_v() in src
    real = Path(".stoa/artifacts/stoa-examples/dfe-special-school-plan-level00.png")
    if real.is_file():  # the live catalogue folder id (checked against /tmp/stoa-uploads on 14 Nov, 22:18)
        assert "a28fcdd13bdb" in privacy.upload_ids(real.read_bytes(), real.name) or privacy._image_norm_v() != "|norm-3"


def test_delete_result_shows_no_server_paths(world, monkeypatch):
    """T2 22:5x: the delete result listed absolute paths ('/home/<login>/work/...'): the operator's login name and
    directory layout, readable by any visitor in the browser's network panel. Roots are named by role now."""
    import getpass
    import json

    r = world["client"].post("/feat/privacy/delete", json={"upload": "upload/0123456789ab/plan.png"})
    text = json.dumps(r.json())
    assert r.json()["ok"] and "<uploads>/0123456789ab" in r.json()["deleted"]
    assert "<team cache>/artifacts/O1/ocr/plan.abc123.json" in r.json()["deleted"]
    assert any("<team cache>/artifacts/llm-cache" in k["where"] for k in r.json()["kept"])
    for bad in ("/home/", "/tmp/", "/data/", str(Path.home()), getpass.getuser()):
        assert bad not in text, bad
    bad_ref = world["client"].post("/feat/privacy/delete", json={"upload": "upload/zzzzzzzzzzzz"})
    assert bad_ref.status_code == 400 and getpass.getuser() not in bad_ref.text
    # the mapper itself: a root keeps the part below it; any other absolute path keeps only its last two parts
    monkeypatch.setenv("JUNCTION_SHARED", "/srv/team/shared")
    assert privacy.public_paths("/srv/team/shared/artifacts/llm-cache, /srv/team/shared-old/x") == \
        "<team cache>/artifacts/llm-cache, …/shared-old/x"
    assert privacy.public_paths("/srv/someone/models/w.pt") == "…/models/w.pt"
    assert privacy.public_paths("could not be removed (permissions)") == "could not be removed (permissions)"


def test_delete_removes_revision_compare_images_of_the_upload(world, tmp_path, monkeypatch):
    """T2 cycle 4: F5's revision compare renders both drawings into <feat cache>/revcompare/<pair key>/ (a.png =
    sheet A, b_aligned.png = sheet B). Keys come from F5's own cached_compare (compare() stubbed: the key is what is
    under test); a pair that involves the deleted upload goes, in either order and on any page; others stay."""
    import pymupdf

    from stoa.features import revcompare as rc

    feat = tmp_path / "feat"
    monkeypatch.setenv("STOA_FEAT_CACHE", str(feat))
    monkeypatch.setattr(rc, "CACHE_DIR", feat / "revcompare")
    monkeypatch.setattr(rc, "UPLOADS", world["folder"].parent)
    monkeypatch.setattr(rc, "_RESULTS", type(rc._RESULTS)())

    def fake_compare(pa, pb, page_a=0, page_b=0, virtual_a=None, virtual_b=None, out_dir=None, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "a.png").write_bytes(b"render of A")
        return {"ok": True}

    monkeypatch.setattr(rc, "compare", fake_compare)
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    doc.save(world["folder"] / "set.pdf")  # a 3-page PDF of the same visitor's set
    third = world["folder"].parent / "c0ffee000000"
    third.mkdir()
    (third / "rev.png").write_bytes(b"a third visitor")
    A, B, C, S = ("upload/0123456789ab/plan.png", "upload/ba9876543210/plan.png", "upload/c0ffee000000/rev.png",
                  "upload/0123456789ab/set.pdf")
    gone = [rc.cached_compare(A, B)["key"], rc.cached_compare(B, A, 0, 0)["key"], rc.cached_compare(S, B, 2)["key"],
            rc.cached_compare(C, S, 0, 1)["key"], rc.cached_compare(A, S, 0, None)["key"]]
    stays = rc.cached_compare(B, C)["key"]
    assert all((feat / "revcompare" / k / "a.png").is_file() for k in gone + [stays])
    # a folder made before F5 bumped the key's version tag (22:5x: 8 -> 9) stays on disk: found too
    import hashlib
    import json

    vs = privacy._rc_versions()
    assert vs[-1] != 30 and vs[-1] >= 9, "the version tag was not found in revcompare.py: update privacy._rc_versions"
    fa, fb = world["folder"] / "plan.png", world["folder"].parent / "ba9876543210" / "plan.png"
    old_key = hashlib.sha256(json.dumps([str(fa.resolve()), fa.stat().st_mtime_ns, fa.stat().st_size, 0,
                                         str(fb.resolve()), fb.stat().st_mtime_ns, fb.stat().st_size, None,
                                         vs[-1] - 1]).encode()).hexdigest()[:16]
    (feat / "revcompare" / old_key).mkdir()
    gone.append(old_key)
    r = privacy.delete_upload("0123456789ab")
    assert r["ok"]
    assert not any((feat / "revcompare" / k).exists() for k in gone), "a compare image of the deleted upload is left"
    assert (feat / "revcompare" / stays / "a.png").is_file(), "another visitors' comparison was removed"
    assert set(rc._RESULTS) == {stays}  # the in-memory results of those pairs are forgotten too
    assert any("DATASET sheet" in k["why"] for k in r["kept"])  # what is not traced is said


def test_delete_stops_jobs_reading_the_upload_and_sweeps_late_readings(world, tmp_path):
    """T2 cycle 4: measured on a live stack, an upload deleted 1 s after arriving got its W3 text layer written 14 s
    later by the warm-up still running. Delete now stops processes whose arguments name a file of the folder (not
    others), and late sweeps remove readings that appear afterwards; a re-upload of the same file is left alone."""
    import subprocess
    import sys

    takeoff = tmp_path / "takeoff"
    os.environ["STOA_TAKEOFF_CACHE"] = str(takeoff)  # restored below
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(world["folder"] / "plan.png")])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(world["other"] / "plan.png")])
    try:
        plan = privacy._late_plan(world["folder"])
        r = privacy.delete_upload("0123456789ab", late=False)
        assert r["ok"] and r["stopped_jobs"] == 1
        assert mine.wait(timeout=5) is not None  # stopped
        assert other.poll() is None  # another upload's job runs on
    finally:
        os.environ.pop("STOA_TAKEOFF_CACHE", None)
        for p in (mine, other):
            p.kill()
            p.wait()
    # a late writer (the warm-up / an answer mid-way) recreates a reading after the Delete
    tl = tmp_path / "shared" / "artifacts" / "W3" / "textlayer"
    tl.mkdir(parents=True)
    late = tl / "plan.0f0f0f0f0f0f0f0f.json"
    late.write_text(f'{{"path": "{world["folder"] / "plan.png"}"}}')
    theirs = tl / "plan.1e1e1e1e1e1e1e1e.json"
    theirs.write_text(f'{{"path": "{world["other"] / "plan.png"}"}}')
    import hashlib

    sha = hashlib.sha256(b"png").hexdigest()[:16]  # plan.png's bytes (the file is gone; F2 names takeoffs like this)
    assert sha in {c["sha"] for c in plan["content"]}
    late_takeoff = takeoff / f"{sha}-p0"  # a takeoff that was running finishes after the Delete
    late_takeoff.mkdir(parents=True)
    assert sorted(privacy.late_sweep(plan)) == sorted([str(late), str(late_takeoff)])
    assert not late.exists() and theirs.exists() and not late_takeoff.exists()
    twin = world["folder"].parent / "abcabcabcabc"  # another visitor uploads the same bytes: its takeoff stays
    twin.mkdir()
    (twin / "copy.png").write_bytes(b"png")
    late_takeoff.mkdir()
    assert privacy.late_sweep(plan) == [] and late_takeoff.exists()
    import shutil as _sh

    _sh.rmtree(twin)
    world["folder"].mkdir()  # uploaded again: its readings are live, the sweep does nothing
    late.write_text(f'{{"path": "{world["folder"] / "plan.png"}"}}')
    assert privacy.late_sweep(plan) == [] and late.exists()


def _tiny_ifc(path, room="Secret Kitchen"):
    """A real IFC4 file: project / site / building / storey / one named space / one door."""
    import ifcopenshell
    import ifcopenshell.guid as guid

    f = ifcopenshell.file(schema="IFC4")
    proj = f.create_entity("IfcProject", GlobalId=guid.new(), Name="T2 test")
    site = f.create_entity("IfcSite", GlobalId=guid.new(), Name="Site")
    bldg = f.create_entity("IfcBuilding", GlobalId=guid.new(), Name="House")
    st = f.create_entity("IfcBuildingStorey", GlobalId=guid.new(), Name="Ground floor", Elevation=0.0)
    sp = f.create_entity("IfcSpace", GlobalId=guid.new(), Name="101", LongName=room)
    door = f.create_entity("IfcDoor", GlobalId=guid.new(), Name="D1")
    for a, b in ((proj, site), (site, bldg), (bldg, st), (st, sp)):
        f.create_entity("IfcRelAggregates", GlobalId=guid.new(), RelatingObject=a, RelatedObjects=[b])
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=guid.new(), RelatingStructure=st,
                    RelatedElements=[door])
    path.parent.mkdir(parents=True, exist_ok=True)
    f.write(str(path))


def test_delete_removes_an_uploaded_models_ifc_index_and_parts(world, tmp_path, monkeypatch):
    """T2 cycle 5, live probe: Delete of an uploaded .ifc left W1's pickled index of the whole model (its spaces'
    names, elements, geometry and the upload's path) in artifacts/W1/ifc_index + its parts map in ifc_parts. Readings
    are made here by W1's own load_index / nested_parts: the current version, an older INDEX_VERSION, an index made
    when the file had another mtime (uploaded again), and one a parse still running writes after the Delete. Another
    visitor's model of the same name keeps its readings."""
    pytest.importorskip("ifcopenshell")
    import shutil as _sh

    from stoa.solvers.ifc import index as w1

    model, theirs = world["folder"] / "house.ifc", world["other"] / "house.ifc"
    _tiny_ifc(model)
    _tiny_ifc(theirs, room="Their Kitchen")
    idx_dir = tmp_path / "shared" / "artifacts" / "W1" / "ifc_index"
    parts_dir = idx_dir.parent / "ifc_parts"

    def read(p):
        w1._MEM.pop(str(p), None)
        w1.load_index(str(p))
        w1.nested_parts(str(p))
        key = w1._cache_key(p)
        return [idx_dir / key, parts_dir / (key[:-4] + ".json")]

    monkeypatch.setattr(w1, "INDEX_VERSION", w1.INDEX_VERSION - 1)
    older = read(model)  # made before W1 bumped the version
    monkeypatch.undo()
    monkeypatch.setenv("STOA_UPLOADS", str(world["folder"].parent))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    st = model.stat()
    os.utime(model, (st.st_atime, st.st_mtime - 3600))
    stale = read(model)  # the same file uploaded an hour earlier (other mtime: other key)
    os.utime(model, (st.st_atime, st.st_mtime))
    mine = read(model)
    kept = read(theirs)
    assert b"Secret Kitchen" in mine[0].read_bytes() and len({*older, *stale, *mine}) == 6
    older[0].unlink()  # an older index removed (cache cleared) while its parts map stays: found by exact name only
    plan = privacy._late_plan(world["folder"])
    r = privacy.delete_upload("0123456789ab", late=False)
    assert r["ok"]
    assert not any(p.exists() for p in older + stale + mine)
    assert all(p.exists() for p in kept)
    assert str(model) not in w1._MEM and str(theirs) in w1._MEM  # this process forgot the parsed model too
    # a parse that was running finishes after the Delete (names recorded at Delete time; the file is gone)
    for p, src in zip(mine, kept):
        _sh.copy(src, p)
    p_tmp = mine[0].with_suffix(".tmp4242")
    p_tmp.write_bytes(b"partial")
    assert sorted(privacy.late_sweep(plan)) == sorted(map(str, mine + [p_tmp]))
    assert all(p.exists() for p in kept)
    world["folder"].mkdir()  # uploaded again: its readings are live, the sweep does nothing
    _sh.copy(kept[0], mine[0])
    assert privacy.late_sweep(plan) == [] and mine[0].exists()


def test_delete_removes_this_browsers_check_list_ticks(world, tmp_path, monkeypatch):
    """T2 cycle 5: F6's check list files a person's ticks per sheet page + browser id in <tmp>/stoa-checklist under a
    hash of (virtual path, page, visitor); the ticks hold no path, so Delete finds them by F6's own _ck_path for the
    ids the browser sends + 'anon'. Another upload's ticks and another browser's stay (listed as kept)."""
    from stoa.features import checklist as f6

    monkeypatch.setenv("STOA_CHECKLIST_DIR", str(tmp_path / "ck"))
    mine = "upload/0123456789ab/plan.png"
    f6.set_check(mine, 0, "vmine12345", "r1", True)
    f6.set_check(mine, 0, None, "r2", True)  # no id: 'anon'
    f6.set_check(mine, 0, "vother9999", "r3", True)  # another browser, id not sent
    f6.set_check("upload/ba9876543210/plan.png", 0, "vmine12345", "r1", True)
    ours = [f6._ck_path(mine, 0, "vmine12345"), f6._ck_path(mine, 0, "anon")]
    keep = [f6._ck_path(mine, 0, "vother9999"), f6._ck_path("upload/ba9876543210/plan.png", 0, "vmine12345")]
    r = world["client"].post("/feat/privacy/delete",
                             json={"upload": "0123456789ab", "visitors": ["vmine12345", "../../etc", "x" * 99]}).json()
    assert r["ok"]
    assert not any(p.exists() for p in ours) and all(p.exists() for p in keep)
    assert any("another browser" in k["why"] for k in r["kept"])
    # an old client that sends no visitors still removes the 'anon' ticks
    f6.set_check("upload/ba9876543210/plan.png", 0, None, "r9", True)
    assert privacy.delete_upload("ba9876543210", late=False)["ok"]
    assert not f6._ck_path("upload/ba9876543210/plan.png", 0, "anon").exists()


def test_delete_forgets_the_api_processs_cached_readings_of_the_upload(world):
    """T2 cycle 5: the API process keeps readings per file in memory (xref sheet indexes keyed by the real path, trust
    maps keyed by a hash of the content, W1's parsed models by path, ...). Delete drops this upload's entries only."""
    from stoa.features import trust_map as tm
    from stoa.features import xref as xr

    mine, theirs = world["folder"] / "plan.png", world["other"] / "plan.png"
    k_mine, k_theirs = tm._file_key(str(mine), 0, "auto"), tm._file_key(str(theirs), 0, "auto")
    tm._CACHE[k_mine + "|ink"] = {"x": 1}
    tm._CACHE[k_theirs] = {"x": 2}
    xr._CACHE[("idx", os.path.realpath(mine), 3, 0)] = ["sheet"]
    xr._CACHE[("idx", os.path.realpath(theirs), 15, 0)] = ["sheet"]
    try:
        r = privacy.delete_upload("0123456789ab", late=False)
        assert r["ok"] and r.get("forgotten_in_api") == 2
        assert k_mine + "|ink" not in tm._CACHE and k_theirs in tm._CACHE
        assert ("idx", os.path.realpath(mine), 3, 0) not in xr._CACHE
        assert ("idx", os.path.realpath(theirs), 15, 0) in xr._CACHE
        # a request that was mid-way caches the sheet index after the Delete: the late sweep forgets it too
        world["folder"].mkdir()
        (world["folder"] / "plan.png").write_bytes(b"png")
        plan = privacy._late_plan(world["folder"])
        plan["memory"] = privacy._memory_plan(world["folder"], [world["folder"] / "plan.png"])
        import shutil as _sh

        _sh.rmtree(world["folder"])
        xr._CACHE[("idx", os.path.realpath(mine), 3, 0)] = ["late"]
        privacy.late_sweep(plan)
        assert ("idx", os.path.realpath(mine), 3, 0) not in xr._CACHE
        assert ("idx", os.path.realpath(theirs), 15, 0) in xr._CACHE
    finally:
        tm._CACHE.pop(k_theirs, None)
        xr._CACHE.pop(("idx", os.path.realpath(theirs), 15, 0), None)


def test_note_says_when_a_service_forwards_outside(world, monkeypatch):
    """T2 cycle 6: a detector / OCR / local-model URL that is a port of this machine forwarding outside (an ssh tunnel;
    V1's det_server with DET_REMOTE_URL) is named in the note while it is configured; a plain local one is not."""
    from stoa import netguard

    monkeypatch.setattr(netguard, "relays", lambda refresh=False: {8148: "forwards outside: DET_REMOTE_URL -> ssh tunnel"})
    for k in ("STOA_OCR_URL", "LLM_BASE_URL", "LLM2_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STOA_DET_URL", "http://127.0.0.1:8143")
    r = world["client"].get("/feat/privacy/note").json()
    assert r["forwarding"] == [] and "outside this server; with Private on it is not used" not in r["text"]
    n = len(r["items"])
    monkeypatch.setenv("STOA_DET_URL", "http://127.0.0.1:8148")
    r = world["client"].get("/feat/privacy/note").json()
    assert [f["env"] for f in r["forwarding"]] == ["STOA_DET_URL"]
    assert "door / window detector this server uses passes its work to a machine outside this server" in r["text"]
    assert len(r["items"]) == n + 1
    item = next(i for i in r["items"] if "door / window detector" in i["what"])
    assert "outside this server" in item["where"] and "refused" in item["where"]
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8148/v1")  # same port by name: named too
    assert len(world["client"].get("/feat/privacy/note").json()["forwarding"]) == 2


def test_late_sweep_removes_a_folder_recreated_only_by_work_files(world):
    """T2 cycle 7 (F7 01:26): F7 keeps the last raw reading in <upload folder>/.stoa_raw/ for 15 min; a correction that
    finishes after the Delete re-creates the folder with only that inside. That is not a re-upload: the sweep removes it.
    A folder with a real file again is a re-upload and stays."""
    plan = privacy._late_plan(world["folder"])
    assert privacy.delete_upload("0123456789ab", late=False)["ok"]
    raw = world["folder"] / ".stoa_raw"
    raw.mkdir(parents=True)
    (raw / "v1-q.json").write_text('{"value": 3}')
    assert str(world["folder"]) in privacy.late_sweep(plan)
    assert not world["folder"].exists()
    raw.mkdir(parents=True)  # a re-upload: the file AND a work folder
    (raw / "v1-q.json").write_text('{"value": 3}')
    (world["folder"] / "plan.png").write_bytes(b"png")
    assert privacy.late_sweep(plan) == [] and (world["folder"] / "plan.png").exists() and raw.exists()


def test_note_says_when_answers_are_saved_on_disk(world, monkeypatch):
    """T2 cycle 8: F7's corrections hook saves each answer it wraps in <upload>/.stoa_raw/ for RAW_TTL (15 min), so the
    note may not say 'not written to disk' on such a tree; on a tree whose corrections module saves nothing it still does."""
    from stoa.features import corrections

    def qa():
        r = world["client"].get("/feat/privacy/note").json()
        return next(i for i in r["items"] if i["what"] == "Your questions and our answers")

    monkeypatch.setattr(corrections, "_raw_put", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(corrections, "RAW_TTL", 900.0, raising=False)
    i = qa()
    assert "not written to disk" not in i["where"]
    assert "on disk" in i["where"] and "upload's folder" in i["where"] and "not your wording" in i["where"]
    assert "Delete my files" in i["kept"] and "15 minutes" in i["kept"] and "not on a timer" in i["kept"]
    monkeypatch.delattr(corrections, "_raw_put", raising=False)
    assert "not written to disk" in qa()["where"]
    from stoa.features.privacy import NOTE
    assert next(n for n in NOTE if n["what"] == "Your questions and our answers")["where"].endswith("not written to disk")


def test_delete_on_an_example_sheet_forgets_this_browsers_corrections_only(world, monkeypatch, tmp_path):
    """T2 cycle 8: F7 files corrections on an example sheet INSIDE its shared folder (seen live: 5 example folders hold
    .stoa_corrections.json / .stoa_raw). The folder stays; this browser's items go, other visitors' stay."""
    import json as _json

    from stoa.features import corrections

    ex = tmp_path / "examples"
    ex.mkdir()
    (ex / "plan.png").write_bytes(b"png")
    monkeypatch.setenv("STOA_SAMPLE_DIRS", str(ex))
    proj = "upload/0123456789ab"
    assert corrections.store_path(proj) == world["folder"] / corrections._FILE
    items = [{"id": "a1", "visitor": "vis-me", "kind": "symbol"}, {"id": "a2", "visitor": "vis-me", "kind": "scale"},
             {"id": "b1", "visitor": "vis-other", "kind": "symbol"}, {"id": "c1", "kind": "scale"}]
    corrections.save(proj, {"version": 1, "project": proj, "rev": 3, "items": items})
    monkeypatch.setenv("STOA_PRIVACY_SAMPLE_FORGET", "0")  # kill switch: nothing touched
    r = world["client"].post("/feat/privacy/delete", json={"upload": f"{proj}/plan.png", "visitors": ["vis-me"]}).json()
    assert r["sample"] and "forgot_corrections" not in r
    monkeypatch.delenv("STOA_PRIVACY_SAMPLE_FORGET")
    lists = {n: world["folder"] / n for n in (".stoa_project.json", ".stoa_project.v-vis-me-0001.json",
                                               ".stoa_project.v-vis-other-01.json")}
    for f in lists.values():
        f.write_text("{}")
    r = world["client"].post("/feat/privacy/delete", json={"upload": f"{proj}/plan.png", "visitors": []}).json()
    assert r["sample"] and "forgot_corrections" not in r and "forgot_project_lists" not in r  # no id: nothing told apart
    r = world["client"].post("/feat/privacy/delete", json={"upload": f"{proj}/plan.png",
                                                           "visitors": ["vis-me-0001", "../x", "a"]}).json()
    assert r["forgot_project_lists"] == 1 and not lists[".stoa_project.v-vis-me-0001.json"].exists()
    assert lists[".stoa_project.json"].exists() and lists[".stoa_project.v-vis-other-01.json"].exists()
    r = world["client"].post("/feat/privacy/delete", json={"upload": f"{proj}/plan.png",
                                                           "visitors": ["vis-me", "anon", ""]}).json()
    assert r["ok"] and r["deleted"] == [] and "nothing you uploaded" in r["sample"] and r["forgot_corrections"] == 2
    left = _json.loads((world["folder"] / corrections._FILE).read_text())["items"]
    assert [i["id"] for i in left] == ["b1", "c1"] and world["folder"].exists() and (world["folder"] / "plan.png").exists()
    r = world["client"].post("/feat/privacy/delete", json={"upload": f"{proj}/plan.png", "visitors": ["vis-me"]}).json()
    assert "forgot_corrections" not in r  # nothing of this browser's left


def _turned_pdf(path):
    """A one-page PDF whose text is drawn turned 90 deg (a re-plot without a /Rotate flag): F5 makes an upright copy."""
    import fitz

    doc = fitz.open()
    pg = doc.new_page(width=842, height=595)
    for i in range(8):
        pg.insert_text((100 + 40 * i, 500), f"ROOM {101 + i} SECRET KITCHEN 35.5 m2", fontsize=11, rotate=90)
    doc.save(str(path))
    doc.close()


def test_delete_removes_the_upright_copy_revcompare_made_of_a_turned_upload(world, tmp_path, monkeypatch):
    """T2 cycle 9: d33's revcompare re-plots a turned PDF page upright into <feat cache>/revcompare/upright/<key>.pdf, a
    full vector copy of the uploaded page (seen live for upload 23ba2beeccb8); Delete left it behind."""
    from stoa.features import revcompare

    if not hasattr(revcompare, "_upright_copy"):
        pytest.skip("revcompare without upright copies")
    feat = tmp_path / "feat"
    monkeypatch.setenv("STOA_FEAT_CACHE", str(feat))
    monkeypatch.setattr(revcompare, "CACHE_DIR", feat / "revcompare")
    f = world["folder"] / "school-upside-down.pdf"
    _turned_pdf(f)
    other = world["other"] / "school-upside-down.pdf"
    _turned_pdf(other)
    got = revcompare._upright_copy(str(f), 0)
    assert got is not None, "the test sheet must read as turned"
    copy = Path(got[0])
    assert copy.is_file() and copy.parent == feat / "revcompare" / "upright"
    theirs = Path(revcompare._upright_copy(str(other), 0)[0])
    assert copy in privacy.derived(world["folder"])[0]
    r = privacy.delete_upload("upload/0123456789ab/school-upside-down.pdf", late=False)
    assert r["ok"] and not copy.exists() and str(copy) in r["deleted"]
    assert theirs.exists()  # another upload's upright copy stays
    # a compare still running at Delete time writes it again: the late sweep removes it (named before the Delete)
    f2 = tmp_path / "uploads" / "00aa11bb22cc" / "x.pdf"
    f2.parent.mkdir()
    _turned_pdf(f2)
    plan = privacy._late_plan(f2.parent)
    late = Path(revcompare._upright_copy(str(f2), 0)[0])
    import shutil as _sh

    _sh.rmtree(f2.parent)
    assert str(late) in privacy.late_sweep(plan) and not late.exists()


def test_delete_removes_the_png_copy_of_a_picture_read_from_the_side_cache(world, tmp_path, monkeypatch):
    """T2 cycle 9: d33's normalize.readable_picture (P1) reads 16-bit grey / transparent / TIFF pictures from an 8-bit
    PNG copy in $STOA_CACHE/formats/<sha1(path)[:12]>/ (not the upload folder): Delete must remove that copy too."""
    from stoa.formats import normalize

    if not hasattr(normalize, "readable_picture"):
        pytest.skip("normalize without the side-cache picture copy (P1 8d9bec57)")
    import numpy as np
    from PIL import Image

    monkeypatch.setenv("STOA_CACHE", str(tmp_path / "cache"))
    f = world["folder"] / "scan16.png"
    Image.fromarray((np.arange(64 * 64, dtype=np.uint16).reshape(64, 64) * 16)).save(f)
    o = world["other"] / "scan16.png"
    Image.fromarray(np.full((64, 64), 4000, dtype=np.uint16)).save(o)
    png, _ = normalize.readable_picture(f)
    theirs, _ = normalize.readable_picture(o)
    side = Path(png).parent
    assert Path(png).is_file() and side.parent == tmp_path / "cache" / "formats"
    assert side in privacy.derived(world["folder"])[0]
    r = privacy.delete_upload("upload/0123456789ab/scan16.png", late=False)
    assert r["ok"] and not side.exists() and str(side) in r["deleted"]
    assert Path(theirs).is_file()  # another upload's copy stays


def test_delete_forgets_this_browsers_measure_fixes_in_a_shared_takeoff(tmp_path, monkeypatch):
    """T2 cycle 9: F2 files each browser's Measure fixes inside the content-keyed takeoff folder. When that folder stays
    (the same bytes open as another upload, or an example sheet), this browser's fix file still goes."""
    from stoa.features import takeoff

    if not hasattr(takeoff, "_fixes_path"):
        pytest.skip("takeoff without per-visitor fixes")
    up, tko, ex = tmp_path / "uploads", tmp_path / "takeoff", tmp_path / "examples"
    tko.mkdir()
    monkeypatch.setenv("STOA_UPLOADS", str(up))
    monkeypatch.setenv("JUNCTION_SHARED", str(tmp_path / "shared"))
    monkeypatch.setenv("STOA_TAKEOFF_CACHE", str(tko))
    mine, twin = up / "0123456789ab", up / "ba9876543210"
    mine.mkdir(parents=True)
    twin.mkdir()
    (mine / "plan.pdf").write_bytes(b"one drawing")
    (twin / "school.pdf").write_bytes(b"one drawing")
    job = tko / takeoff.job_key(str(mine / "plan.pdf"), 0)
    job.mkdir()
    me, other = takeoff._fixes_path(job, "vis-me-0001"), takeoff._fixes_path(job, "vis-other-01")
    assert me != other and me.parent == job
    for p in (me, other, job / "result.json"):
        p.write_text("{}")
    r = privacy.delete_upload("upload/0123456789ab/plan.pdf", late=False, visitors=["vis-me-0001", "anon"])
    assert r["ok"] and job.exists() and not me.exists() and other.exists() and (job / "result.json").exists()
    # an example sheet: its folder stays, this browser's fixes on its takeoff go
    me.write_text("{}")
    (ex / "school.pdf").parent.mkdir()
    (ex / "school.pdf").write_bytes(b"one drawing")
    monkeypatch.setenv("STOA_SAMPLE_DIRS", str(ex))
    r = privacy.delete_upload("upload/ba9876543210/school.pdf", visitors=["vis-me-0001"])
    assert r["sample"] and r["forgot_takeoff_fixes"] == 1 and not me.exists() and other.exists() and twin.exists()
    monkeypatch.setenv("STOA_PRIVACY_SAMPLE_FORGET", "0")
    me.write_text("{}")
    r = privacy.delete_upload("upload/ba9876543210/school.pdf", visitors=["vis-me-0001"])
    assert r["sample"] and "forgot_takeoff_fixes" not in r and me.exists()
