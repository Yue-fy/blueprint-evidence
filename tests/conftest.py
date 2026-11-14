"""Skip the tests that need the Stoa dataset or the local model server when they are not there.

Most dataset tests already skip themselves; the ones listed here read dataset files (DXF siblings, questions.jsonl)
without a marker. The list is explicit on purpose: a test that fails for another reason still fails.
Run everything: set STOA_DATA to the dataset root (with questions.jsonl) and start the local model server
(serving/start.sh) without STOA_MODE=api.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

NEEDS_DATASET = {
    "tests/test_foundation.py::test_text_fuzzy_ignores_rows_without_gold",
    "tests/test_p1_portability.py::test_cli_answer_batch_and_resume",
    "tests/test_router.py::test_route_orders_deterministic_first",
    "tests/test_vector_dxf.py::test_dxf_storey_count_is_the_same_in_every_language",
    "tests/test_vector_dxf.py::test_dxf_objects_of_one_named_room",
    "tests/test_vector_dxf.py::test_dxf_room_connections_from_door_sides",
    "tests/test_vector_dxf.py::test_dxf_plan_vs_dxf_room_area",
    "tests/test_vector_dxf.py::test_dxf_route_counts_doors_on_the_shortest_walk",
    "tests/test_vector_dxf.py::test_dxf_label_from_another_floor_is_read_as_its_concept",
    "tests/test_vector_dxf.py::test_one_letter_typos_in_object_nouns_are_read_as_meant",
}
# need the local text model (translation of the question) or the local-server fallback logic
NEEDS_LOCAL_MODEL = {
    "tests/test_ifc_c9.py::test_bare_where_is_room[Wo ist Raum 103?]",
    "tests/test_ifc_c9.py::test_bare_where_is_room[Kus asub ruum 103?]",
    "tests/test_llm_parsing.py::test_local2_down_falls_back_to_local_at_once",
}
# read a file kept outside this repository (the team's anonymised upload copies; path relative to the repo root):
# skipped when the file is not there
NEEDS_FILE = {
    "tests/test_g1_stated_count.py::test_pipe_runs_route_only_text_questions": ".stoa/neutral/x1sl/acf1f07a2d/acf1f07a2d.pdf",
}

# test the local-backend code paths with a mocked client: STOA_MODE=api would reroute "local" to the hosted backend,
# so these run with STOA_MODE unset (they make no network call either way)
LOCAL_BACKEND_LOGIC = {
    "tests/test_p1_portability.py::test_local_llm_never_seen_up_fails_fast",
}


# Tests of the hosted-step logic with a stubbed client: they assume the Gemini step is allowed, so a shell that turns
# it off (STOA_GEMINI_ESCALATE=0, as the team's own test runs did) must not leak into them.
HOSTED_STEP_LOGIC = {
    "tests/test_piping.py::test_connection_uses_validated_gemini_evidence_ids_when_key_exists",
}


@pytest.fixture(autouse=True)
def _local_backend_logic(request, monkeypatch):
    if request.node.nodeid in LOCAL_BACKEND_LOGIC:
        monkeypatch.delenv("STOA_MODE", raising=False)
    if request.node.nodeid in HOSTED_STEP_LOGIC:
        monkeypatch.delenv("STOA_GEMINI_ESCALATE", raising=False)
        monkeypatch.delenv("STOA_PRIVATE", raising=False)


def _has_dataset() -> bool:
    from stoa.data import data_root

    return (data_root() / "questions.jsonl").is_file()


def pytest_collection_modifyitems(config, items):
    dataset = _has_dataset()
    api_mode = os.environ.get("STOA_MODE", "").strip().lower() == "api"
    for item in items:
        if not dataset and item.nodeid in NEEDS_DATASET:
            item.add_marker(pytest.mark.skip(reason="needs the Stoa dataset (STOA_DATA with questions.jsonl)"))
        if item.nodeid in NEEDS_FILE and not (Path(config.rootpath) / NEEDS_FILE[item.nodeid]).is_file():
            item.add_marker(pytest.mark.skip(reason=f"needs {NEEDS_FILE[item.nodeid]} (not in this repository)"))
        if api_mode and item.nodeid in NEEDS_LOCAL_MODEL:
            item.add_marker(pytest.mark.skip(reason="needs the local model server (STOA_MODE=api is set)"))
