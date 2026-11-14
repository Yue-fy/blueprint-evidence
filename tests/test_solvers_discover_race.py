"""S0 (RL 18:21): requests that overlap the first solver discovery must wait for it, not see a partial registry."""
import threading
import time
import types

import stoa.solvers as S


def test_overlapping_requests_wait_for_first_discovery(monkeypatch):
    reg: dict = {}
    names = [f"fake{i}" for i in range(6)]
    monkeypatch.setattr(S, "_REGISTRY", reg)
    monkeypatch.setattr(S, "_IMPORT_ERRORS", {})
    monkeypatch.setattr(S, "_DISCOVERED", False)
    monkeypatch.setattr(S, "_modules", lambda: [types.SimpleNamespace(name=f"stoa.solvers.{n}") for n in names])

    def slow_import(name):
        time.sleep(0.05)
        short = name.rsplit(".", 1)[-1]
        S.get_solver  # noqa: B018 - module attribute access only
        S.discover()  # a module calling discover/get_solver at import time must not recurse or deadlock
        reg[short] = type(short, (), {})

    monkeypatch.setattr(S.importlib, "import_module", slow_import)
    errors, seen = [], []
    first = threading.Thread(target=S.available)
    first.start()
    time.sleep(0.02)

    def ask():
        try:
            seen.append(type(S.get_solver("fake5")).__name__)
        except KeyError as e:  # pragma: no cover - the bug
            errors.append(str(e)[:60])

    others = [threading.Thread(target=ask) for _ in range(3)]
    for t in others:
        t.start()
    for t in [first, *others]:
        t.join(5)
    assert errors == []
    assert seen == ["fake5"] * 3
    assert S.available() == sorted(names)
