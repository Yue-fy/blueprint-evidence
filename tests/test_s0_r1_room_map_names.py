"""T2 21:15: CC5K room maps of non-dataset drawings are named by content, not by file name.

Before: experiments/r1/room_graph/graph.py looked up artifacts/R1/cc5k_seg_live/<stem>.npz, so every upload named
plan.png (5 different ones on the demo machine) got the FIRST plan.png's rooms, counts and evidence boxes: wrong answers
and one visitor's drawing data served to another."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from stoa.extras.room_graph import graph


def _img(path: Path, shade: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (shade, shade, shade)).save(path)
    return path


def test_dataset_files_keep_their_stem(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_DATA", str(tmp_path / "data"))
    assert graph.map_name("data/cubicasa5k/x/F1_scaled.png") == "F1_scaled"
    p = _img(tmp_path / "data" / "cubicasa5k" / "abc" / "F1_scaled.png", 10)
    assert graph.map_name(str(p)) == "F1_scaled"


def test_uploads_with_the_same_name_get_different_maps(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_DATA", str(tmp_path / "data"))
    a = _img(tmp_path / "up" / "aaaaaaaaaaaa" / "plan.png", 10)
    b = _img(tmp_path / "up" / "bbbbbbbbbbbb" / "plan.png", 200)
    a2 = _img(tmp_path / "up" / "cccccccccccc" / "plan.png", 10)  # same content as a
    na, nb, na2 = graph.map_name(str(a)), graph.map_name(str(b)), graph.map_name(str(a2))
    assert na.startswith("plan.") and na != nb and na == na2


def test_a_stem_only_map_of_another_drawing_is_not_served(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(graph, "ART", tmp_path / "R1")
    live = tmp_path / "R1" / "cc5k_seg_live"
    live.mkdir(parents=True)
    np.savez_compressed(live / "plan.npz", rooms=np.zeros((4, 4)), icons=np.zeros((4, 4)))  # another visitor's
    mine = _img(tmp_path / "up" / "dddddddddddd" / "plan.png", 77)
    assert graph._npz(str(mine), live=False) is None
    np.savez_compressed(live / f"{graph.map_name(str(mine))}.npz", rooms=np.ones((4, 4)), icons=np.zeros((4, 4)))
    assert graph._npz(str(mine), live=False) == live / f"{graph.map_name(str(mine))}.npz"


@pytest.mark.skipif(not Path(os.environ.get("CC5K_PYTHON", ".venv-cc5k/bin/python")).exists(),
                    reason="no CC5K torch env on this machine")
def test_a_live_run_saves_the_map_under_the_content_name(tmp_path, monkeypatch):
    monkeypatch.setenv("STOA_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(graph, "ART", tmp_path / "R1")
    (tmp_path / "R1" / "cc5k_seg_live").mkdir(parents=True)
    img = Image.new("RGB", (256, 192), "white")
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 236, 172), outline="black", width=4)
    d.line((128, 20, 128, 172), fill="black", width=4)
    p = tmp_path / "up" / "eeeeeeeeeeee" / "plan.png"
    p.parent.mkdir(parents=True)
    img.save(p)
    f = graph._npz(str(p), live=True)
    if f is None:
        pytest.skip("CC5K live run unavailable (weights / env)")
    assert f.name == f"{graph.map_name(str(p))}.npz" and f.name != "plan.npz"
