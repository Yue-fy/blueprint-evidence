"""RL 20:21 / audit risk 3: model-written table code must not reach files through the real `pd`."""

import pandas as pd
import pytest

from stoa.solvers.vector.tableqa import _run

T = {"df": pd.DataFrame({"room": ["A", "B"], "area": [10.0, 12.5]})}


@pytest.mark.parametrize("code", [
    "x = pd.read_csv('/etc/hostname')\nanswer = len(x)",
    "x = pd.read_pickle('/tmp/x.pkl')\nanswer = len(x)",
    "df.to_csv('/tmp/leak.csv')\nanswer = len(df)",
    "x = pd.io.parsers.read_csv\nanswer = len(df)",
    "f = getattr(pd, 'read_' + 'csv')\nanswer = len(df)",
])
def test_file_io_is_refused(code):
    with pytest.raises(ValueError, match="forbidden"):
        _run(code, T)


def test_plain_table_code_still_runs():
    out = _run("answer = df[df['room'] == 'B']['area'].sum()", T)
    assert "answer" in str(out) or out
    out = _run("answer = len(df[pd.api.types.is_numeric_dtype(df['area']) & (df['area'] > 11)])", T)
    assert out
