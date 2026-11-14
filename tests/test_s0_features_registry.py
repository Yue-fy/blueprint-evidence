"""S0: the F1-F8 feature registry mounts only what STOA_FEATURES names and fails closed."""

from __future__ import annotations

import sys
import types

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import stoa.features as feats


def test_default_off_mounts_nothing():
    app = FastAPI()
    assert feats.include_feature_routers(app, env="") == []
    body = TestClient(app).get("/feat").json()
    assert body["mounted"] == []
    assert set(body["features"]) == set(feats.FEATURES)
    assert set(body["features"].values()) == {"off"}


def test_enabled_parsing():
    assert feats.enabled("") == []
    assert feats.enabled(" takeoff , nope ") == ["takeoff"]
    assert feats.enabled("all") == feats.FEATURES


def test_mounts_a_feature_and_fails_closed(monkeypatch):
    good = types.ModuleType("stoa.features.takeoff")
    r = APIRouter(prefix="/feat/takeoff")

    @r.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    good.router = r
    bad = types.ModuleType("stoa.features.region")  # no router
    monkeypatch.setitem(sys.modules, "stoa.features.takeoff", good)
    monkeypatch.setitem(sys.modules, "stoa.features.region", bad)

    def boom(name, *a, **k):
        if name == "stoa.features.selfcheck":
            raise RuntimeError("broken feature")
        return orig(name, *a, **k)

    orig = feats.importlib.import_module
    monkeypatch.setattr(feats.importlib, "import_module", boom)
    app = FastAPI()
    assert feats.include_feature_routers(app, env="takeoff,region,selfcheck") == ["takeoff"]
    c = TestClient(app)
    assert c.get("/feat/takeoff/ping").json() == {"ok": True}
    st = c.get("/feat").json()["features"]
    assert st["takeoff"] == "mounted"
    assert st["region"].startswith("error") and st["selfcheck"].startswith("error: RuntimeError")
    assert st["batch"] == "off"


def test_api_app_serves_feat_endpoint():
    from stoa.api import app

    assert any(getattr(rt, "path", None) == "/feat" for rt in app.routes)


def test_web_only_feature_is_not_an_error():
    from fastapi import FastAPI

    import stoa.features as feats

    app = FastAPI()
    feats.include_feature_routers(app, env="batch")
    assert feats.STATUS["batch"] == "web-only"
