"""O1 c14: sheet-consensus OCR spelling repair (stoa.ocr.sheet_vocab)."""

import pytest

from stoa import ocr
from stoa.ocr import _dl1, _respell, quality, sheet_vocab
from stoa.solvers.raster.textlayer import TextItem

needs_dict = pytest.mark.skipif(quality.words() is None, reason="no English word list on this machine")


def _items(lines):
    return [TextItem(t, [0, 10 * k, 50, 10 * k + 8]) for k, t in enumerate(lines)]


def test_dl1():
    assert _dl1("engilsh", "english")  # adjacent swap
    assert _dl1("eaglish", "english")  # substitution
    assert _dl1("offic", "office") and _dl1("stoore", "store")
    assert not _dl1("english", "english") and not _dl1("cng!lsh", "english") and not _dl1("lift", "left2")


def test_respell_keeps_case_and_splits_a_glued_letter():
    assert _respell("Engilsh", "English") == "English"
    assert _respell("Restrcoms", "RESTROOMS") == "Restrooms"
    assert _respell("riyer", "RIVER") == "river"
    assert _respell("SCALEA", "SCALE") == "SCALE A"
    assert _respell("OFRAME", "FRAME") == "O FRAME"
    assert _respell("STANDD", "STAND") == "STAND"  # a stutter: dropped


@needs_dict
def test_efa_scan_room_tags():
    # X1 00:27: EFA colour sheet scan, 9 x 'English N / 55 m2', OCR misread 3 of them; MFL tags glued
    lines = [f"English {k}" for k in (1, 2, 4, 5, 6, 7, 8)] + ["Engilsh 3", "Eaglish 9"] + \
            ["55m2"] * 7 + ["55m12", "MFL1", "MFL 2", "MFL3", "MFL 5", "MFL 3WR", "Stair"]
    out, n = sheet_vocab(_items(lines))
    texts = [i.text for i in out]
    assert "English 3" in texts and "English 9" in texts
    assert "55 m2" in texts and "55m12" not in texts
    assert "MFL 1" in texts and "MFL 3" in texts and "MFL 3WR" in texts
    assert n == 5


@needs_dict
def test_no_repair_without_consensus():
    # a word printed once, or a dictionary word, or two equally close candidates: untouched
    lines = ["Lift", "Left", "Store", "Stores", "Stores", "Stores", "Engilsh 3", "FP1", "FP 2"]
    out, n = sheet_vocab(_items(lines))
    assert [i.text for i in out] == lines and n == 0  # 'Store' is a word; 'English' < 3x; 'FP 2' once only


@needs_dict
def test_kill_switch(monkeypatch):
    monkeypatch.setattr(ocr, "VOCAB", False)
    lines = ["English 1", "English 2", "English 4", "Engilsh 3", "x", "y"]
    out, n = sheet_vocab(_items(lines))
    assert n == 0 and [i.text for i in out] == lines


@needs_dict
def test_layer_richness_judges_the_reading_not_the_repair():
    # sc-347: respelled words must not flip textlayer's 'cached VLM layer is richer' choice
    from stoa.solvers.raster.textlayer import TextLayer, _richness

    read = _items(["Offic", "Oifice", "Circutation", "Office", "Office", "Office", "Circulation", "Circulation",
                   "Circulation"])
    fixed, n = sheet_vocab(read)
    tl = TextLayer(path="x.png", size=(10, 10), items=fixed)
    assert n == 3 and _richness(tl) == 9
    tl.read_items = read
    assert _richness(tl) == 6


def test_typed_rooms_is_no_floor_sum():
    from stoa.solvers.raster.apartment import typed_rooms

    assert typed_rooms("What is the total area of the ICT classrooms?") == "ICT"
    assert typed_rooms("What is the total floor area of the three art rooms (Art 1, Art 2 and Art 3)?") == "art"
    assert typed_rooms("What is the total floor area of the four Business Studies rooms?") == "Studies"
    for q in ("What is the total floor area of all rooms in the entry vestibule module (module 01)?",
              "Sum up all the room areas in the entry vestibule module.",
              "What is the total floor area of all the rooms labelled 'Infants'?",
              "What is the total net floor area of all numbered indoor rooms on this floor?",
              "Wie gross ist die gesamte Nettofläche aller Räume?",
              "What is the total area of all rooms on the ground floor?"):
        assert typed_rooms(q) is None, q
