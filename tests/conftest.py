"""Skip the tests that need the Stoa dataset or the local model server when they are not there.

Most dataset tests already skip themselves; the ones listed here read dataset files (DXF siblings, questions.jsonl)
without a marker. The list is explicit on purpose: a test that fails for another reason still fails.
Run everything: set STOA_DATA to the dataset root (with questions.jsonl) and start the local model server
(serving/start.sh) without STOA_MODE=api.
"""

from __future__ import annotations

import os

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


def _has_dataset() -> bool:
    from stoa.data import data_root

    return (data_root() / "questions.jsonl").is_file()


def pytest_collection_modifyitems(config, items):
    dataset = _has_dataset()
    api_mode = os.environ.get("STOA_MODE", "").strip().lower() == "api"
    for item in items:
        if not dataset and item.nodeid in NEEDS_DATASET:
            item.add_marker(pytest.mark.skip(reason="needs the Stoa dataset (STOA_DATA with questions.jsonl)"))
        if api_mode and item.nodeid in NEEDS_LOCAL_MODEL:
            item.add_marker(pytest.mark.skip(reason="needs the local model server (STOA_MODE=api is set)"))
