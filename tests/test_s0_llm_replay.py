"""STOA_LLM_REPLAY=1 (S0 gate arbitration replay): hosted backends never make a new call; local ones are untouched."""
import pytest

from stoa import llm


def test_replay_blocks_uncached_hosted_call(monkeypatch, tmp_path):
    monkeypatch.setenv("STOA_LLM_REPLAY", "1")
    monkeypatch.setenv("STOA_LLM_CACHE_DIR", str(tmp_path))

    def boom(*a, **k):  # a new call would need a client
        raise AssertionError("replay must not create a client")

    monkeypatch.setattr(llm, "_client", boom)
    with pytest.raises(RuntimeError, match="STOA_LLM_REPLAY"):
        llm.chat("2+2?", backend="gemini", model="gemini-3-flash-preview", cache=True)
    with pytest.raises(RuntimeError, match="STOA_LLM_REPLAY"):
        llm.chat("2+2?", backend="nvidia", model="x", temperature=0.8)


def test_replay_ignores_team_day_cap(monkeypatch):
    from stoa.agent import escalate

    monkeypatch.setenv("STOA_LLM_REPLAY", "1")
    assert escalate._team_calls_today() == 0
