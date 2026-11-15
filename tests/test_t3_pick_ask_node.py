"""T3 regression (2) (M1 03:14, captain 03:58): the project's 'Which one did you mean?' pick must re-ask WITH its
learn + visitor (solver 'pipeline' named), else the card shows the mean of the same-named rooms. Runs the node test
web/src/features/project/pick_ask.test.ts (the real web/src/api.ts request shape + a guard on project/index.ts)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _node() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.(\d+)", v)
    # type stripping (22.18) + module.registerHooks (23.5)
    return node if m and (int(m[1]), int(m[2])) >= (23, 5) else None


@pytest.mark.skipif(_node() is None, reason="needs Node >= 23.5 (type stripping + module.registerHooks)")
def test_project_pick_reask_keeps_learn_and_visitor():
    root = Path(__file__).resolve().parents[1]
    test = root / "web" / "src" / "features" / "project" / "pick_ask.test.ts"
    r = subprocess.run([_node(), "--test", str(test)], capture_output=True, text=True, timeout=120, cwd=root)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    assert "ℹ fail 0" in r.stdout and "ℹ pass 2" in r.stdout
