"""D39: without the dataset the router falls back to stoa/router/train_stats.json (text-free TRAIN statistics)."""

import json
import re

import pytest

from stoa.router import canonical as C
from stoa.router import prior as P
from stoa.router import train_stats as TS
from stoa.router.intent import parse
from stoa.solvers import raster as R


def test_stats_file_is_text_free():
    d = json.loads(TS.PATH.read_text(encoding="utf-8"))
    assert d["prior"] and d["templates"] and d["canonical"] and d["class_prior"]
    assert all(isinstance(p[3], (int, float)) for p in d["prior"])  # no dataset text answers
    assert all(re.fullmatch(r"[0-9a-f]{16}", t) for t in d["templates"])  # hashes, not question text
    assert all(len(q) < 200 for _, _, q in d["canonical"])


@pytest.fixture
def no_split(monkeypatch):
    """The split file is missing (a fresh clone without the dataset): every reader must use the packed stats."""
    import stoa.eval.splits as S

    def missing(name):
        raise FileNotFoundError(f"{name}.jsonl missing")

    monkeypatch.setattr(S, "load_split", missing)
    monkeypatch.setattr(P, "_TABLE", None)
    monkeypatch.setattr(P, "_TEMPLATES", None)
    monkeypatch.setattr(R, "_PRIOR", None)
    C._plain_templates.cache_clear()
    yield
    C._plain_templates.cache_clear()


def test_fallback_without_the_split(no_split):
    tpl = "How many windows are drawn on this floor plan?"
    assert P.known_template(tpl)
    assert not P.known_template("Which room of this plan would suit a grand piano best?")
    it = parse(tpl)
    pa = P.lookup("swiss_dwellings", it)
    assert pa is not None and isinstance(pa.value, (int, float)) and 0 < pa.hit_rate <= 1
    assert C.canonical("swiss_dwellings", parse("Wie viele Fenster sind auf diesem Grundriss zu sehen?")) == tpl
    cp = R.class_prior()
    assert "window" in cp and isinstance(cp["window"][0], (int, float))


def test_stats_match_the_split_when_present():
    """With the split present (team box, dataset tests), the stats file equals what the readers derive from it."""
    from stoa.eval.splits import load_split

    try:
        load_split("train")
    except FileNotFoundError:
        pytest.skip("needs the train split (STOA_DATA + JUNCTION_SHARED)")
    table = {k: b for k, b in P.table().items() if isinstance(b.value, (int, float)) and not isinstance(b.value, bool)}
    packed = TS.prior_table()
    assert set(table) == set(packed)
    for k, b in table.items():
        assert (packed[k].value, packed[k].unit, packed[k].n) == (b.value, b.unit, b.n)
        assert abs(packed[k].hit_rate - b.hit_rate) < 1e-5
    assert C._plain_templates() == TS.canonical()
    assert {c: (v, round(h, 6), n) for c, (v, h, n) in R.class_prior().items()} == TS.class_prior()
