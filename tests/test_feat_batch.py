"""F4 batch Q&A: the web feature's unit tests (question parsing, the .xlsx writer/reader, the ask queue) run under Node.

The feature is frontend-only (web/src/features/batch/): every question goes through the existing /api/ask, so there is
no Python answer code to test here. Skipped when Node >= 22.18 (which strips TypeScript types itself) is not installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "web" / "src" / "features" / "batch" / "batch.test.ts"


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
def test_batch_feature_node_tests():
    r = subprocess.run([_node(), "--test", str(TEST)], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert re.search(r"ℹ pass (\d+)", r.stdout) and "ℹ fail 0" in r.stdout
