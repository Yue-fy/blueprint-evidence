"""F4 convention switch: the web feature's unit tests (rooms from the takeoff rows, wall thickness from the gaps between
room outlines, net / centre-line / gross areas, skirting and door conventions, the export) run under Node.

The feature is frontend-only (web/src/features/conventions/): it recounts F2's takeoff rows (GET /feat/takeoff/status) in
the browser and reads nothing again, so there is no Python code to test here. Skipped when Node >= 22.18 (which strips TypeScript types itself) is not installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "web" / "src" / "features" / "conventions" / "conventions.test.ts"


def _node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.(\d+)", v)
    return node if m and (int(m[1]), int(m[2])) >= (22, 18) else None


@pytest.mark.skipif(_node() is None, reason="needs Node >= 22.18 (type stripping)")
def test_conventions_feature_node_tests():
    r = subprocess.run([_node(), "--test", str(TEST)], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"ℹ pass (\d+)", r.stdout) and "ℹ fail 0" in r.stdout


def test_conventions_is_served_when_stoa_features_names_it():
    """The final swap sets only STOA_FEATURES on :8100 and the web turns on what GET /feat reports 'mounted' or
    'web-only' (registry.ts servedFeatures). A web-only feature missing from FEATURES is never reported, so the
    Conventions tab stayed off even with 'conventions' in STOA_FEATURES (found 01:40, cycle 8)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import stoa.features as feats

    app = FastAPI()
    mounted = feats.include_feature_routers(app, env="takeoff,conventions,batch")
    st = TestClient(app).get("/feat").json()["features"]
    assert st["conventions"] == "web-only" and st["batch"] == "web-only"
    assert "conventions" not in mounted and "takeoff" in mounted
    app2 = FastAPI()
    feats.include_feature_routers(app2, env="takeoff")
    assert TestClient(app2).get("/feat").json()["features"]["conventions"] == "off"
