"""Feature routers F1-F8 (prompts/FEATURES.md, captain Sat 19:54). Owned by S0.

Each feature lives in ``stoa/features/<name>.py`` and exposes a FastAPI ``router`` (prefix ``/feat/<name>``).
S0 has pre-registered every feature's module name in FEATURES below, so a feature agent does NOT need to edit this
file. A feature agent that picks a different module name edits ONLY its own line (no other line: append-only, so
merges stay trivial).

A feature is served only when its name is listed in ``STOA_FEATURES`` (comma list; ``all`` = every feature; default
none), so the captain's stable demo is unchanged until QA has rehearsed the feature and S0 has gated it. A module that
is missing, fails to import, has no ``router`` or fails to mount is skipped (fail closed) and reported at ``GET /feat``;
one broken feature never breaks the API.
"""

from __future__ import annotations

import importlib
import os

FEATURES: list[str] = [
    "region",  # F1 region ask: questions scoped to a dragged box / lasso
    "takeoff",  # F2 auto takeoff: quantity table + CSV / XLSX export
    "selfcheck",  # F3 drawing self-check: inconsistencies inside a sheet / set, with evidence
    "batch",  # F4 batch Q&A: many questions on one sheet / set, live table + export
    "revcompare",  # F5 revision compare: two versions of a sheet, aligned diff + quantity impact
    "trust_map",  # F6 trust map: how reliably each region of the sheet was read
    "corrections",  # F7 learn from corrections: per-project memory of user fixes
    "xref",  # F8 cross-references: clickable callouts / sheet references, answers follow them
    "project",  # T3 uploaded project loop: sheets -> one project, levels, building scope, review list
    "privacy",  # T2 privacy: storage note + Delete my files (audit 19:51 fix #3)
]

# Features that live only in web/src/features (no backend router): reported as "web-only", never as an error.
WEB_ONLY: set[str] = {"batch"}

# name -> "mounted" | "off" | "web-only" | "<error text>"; filled by include_feature_routers, served at GET /feat
STATUS: dict[str, str] = {}


def enabled(env: str | None = None) -> list[str]:
    """Names in FEATURES that STOA_FEATURES (or ``env``) turns on, in registry order."""
    raw = os.environ.get("STOA_FEATURES", "") if env is None else env
    want = {w.strip() for w in raw.split(",") if w.strip()}
    return [f for f in FEATURES if "all" in want or f in want]


def include_feature_routers(app, env: str | None = None) -> list[str]:
    """Mount the enabled features' routers on a FastAPI app and add ``GET /feat``; returns the names mounted."""
    on = set(enabled(env))
    mounted: list[str] = []
    STATUS.clear()
    for name in FEATURES:
        if name not in on:
            STATUS[name] = "off"
            continue
        if name in WEB_ONLY:
            STATUS[name] = "web-only"
            continue
        try:
            mod = importlib.import_module(f"stoa.features.{name}")
            router = getattr(mod, "router", None)
            if router is None:
                STATUS[name] = "error: module has no `router`"
                continue
            app.include_router(router)
        except Exception as exc:  # noqa: BLE001 - fail closed: a broken feature must not break the API
            STATUS[name] = f"error: {type(exc).__name__}: {exc}"[:300]
            continue
        STATUS[name] = "mounted"
        mounted.append(name)

    @app.get("/feat")
    def feat_status() -> dict:
        return {"features": dict(STATUS), "mounted": list(mounted)}

    return mounted
