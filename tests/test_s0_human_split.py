"""S0 tick 25: expert golds typed with a thousands comma (H 23:36: '6,095' ft2 was scored as 6.095)."""
import json

from stoa.eval.splits import _human_number, _human_rows


def test_human_number_thousands_vs_decimal_comma():
    assert _human_number("6,095", "ft2") == 6095
    assert _human_number("1,234,567.5", "m2") == 1234567.5
    assert _human_number("1,200", "count") == 1200
    assert _human_number("20,5", "m2") == 20.5
    assert _human_number("3,450", "m") == 3.45  # metric decimal comma (O1's convention)
    assert _human_number(" 52.28 ", "m2") == 52.28


def test_human_rows_reads_ft2_thousands(tmp_path):
    f = tmp_path / "questions.jsonl"
    f.write_text(json.dumps({"qid": "h-1", "path": "x.png", "question": "Gross area?", "answer": "6,095",
                             "unit": "ft2", "type": "area"}) + "\n"
                 + json.dumps({"qid": "h-2", "path": "x.png", "question": "Name?", "answer": "Lobby"}) + "\n")
    rows = {r["qid"]: r for r in _human_rows(f)}
    assert rows["h-1"]["answer"] == 6095
    assert rows["h-2"]["answer"] == "Lobby"
