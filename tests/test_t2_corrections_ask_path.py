"""T2 cycle 8: POST /feat/corrections/ask must not read a file outside the dataset / uploads (route_probe2 on d32 read a
canary PDF in /tmp: 'SECRET ROOM 101 Kitchen', also through the web proxy). Needs experiments/t2/f7_corrections_ask_path.patch
in stoa/features/corrections.py (F7's file); skips until it is applied."""

from __future__ import annotations

import inspect

import pytest

corrections = pytest.importorskip("stoa.features.corrections")
if corrections.router is None:  # pragma: no cover — fastapi missing
    pytest.skip("no fastapi", allow_module_level=True)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_GUARDED = "STOA_CORRECTIONS_ASK_ANY_PATH" in inspect.getsource(corrections)
pytestmark = pytest.mark.skipif(not _GUARDED, reason="experiments/t2/f7_corrections_ask_path.patch not applied yet")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    calls = []

    def fake_ask_with(solve, path, question, solver_args=None, project=None):
        calls.append(path)  # what would have been solved
        return {"value": "solved", "path": path}

    monkeypatch.setattr(corrections, "ask_with", fake_ask_with)
    monkeypatch.delenv("STOA_CORRECTIONS_ASK_ANY_PATH", raising=False)
    app = FastAPI()
    app.include_router(corrections.router)
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 CANARY")
    return TestClient(app), calls, str(secret)


def _ask(c, path):
    return c.post("/feat/corrections/ask", json={"path": path, "question": "What is the name of room 101?"})


def test_a_path_outside_the_dataset_is_refused(client):
    c, calls, secret = client
    for p in (secret, "file://" + secret, "//" + secret.lstrip("/"), "data/../../" + secret.lstrip("/"),
              "upload/0123456789ab/../../x.pdf", "~/x.pdf", "C:/x.pdf", "data\\..\\x.pdf"):
        r = _ask(c, p)
        assert r.status_code == 400, (p, r.status_code, r.text[:200])
    assert calls == []  # nothing reached a reader


def test_dataset_and_upload_paths_still_answer(client):
    c, calls, _ = client
    for p in ("data/swiss/floor_9385.pdf", "upload/0123456789ab/plan.png"):
        r = _ask(c, p)
        assert r.status_code == 200 and r.json()["value"] == "solved"
    assert calls == ["data/swiss/floor_9385.pdf", "upload/0123456789ab/plan.png"]


def test_kill_switch_restores_the_old_route(client, monkeypatch):
    c, calls, secret = client
    monkeypatch.setenv("STOA_CORRECTIONS_ASK_ANY_PATH", "1")
    assert _ask(c, secret).status_code == 200 and calls == [secret]
