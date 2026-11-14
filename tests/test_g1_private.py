"""Captain 18:53 PRIVATE MODE + 'where the data went' (no hosted call is made in these tests)."""


def test_private_flag_turns_hosted_steps_off(monkeypatch):
    from stoa.pipeline import Pipeline

    monkeypatch.delenv("STOA_PRIVATE", raising=False)
    p = Pipeline(private=1, gemini=1, verify=1, api=1)
    assert (p.private, p.gemini, p.verify, p.api) == (True, False, False, False)
    assert not Pipeline(gemini=0).private


def test_hosted_allowed(monkeypatch):
    from stoa.agent import private as P

    monkeypatch.setenv("STOA_PRIVATE", "1")
    monkeypatch.delenv("STOA_EU_ONLY", raising=False)
    assert P.hosted_allowed("local") and P.hosted_allowed("local2")
    assert not P.hosted_allowed("gemini") and not P.hosted_allowed("nvidia") and not P.hosted_allowed("verda")
    monkeypatch.setenv("STOA_EU_ONLY", "1")
    assert P.hosted_allowed("verda") and not P.hosted_allowed("gemini")


def test_vision_read_refuses_in_private_mode(monkeypatch):
    """vision.read returns before rendering / calling anything, and says so in the trace."""
    import stoa.agent.vision as V
    from stoa.contract import Answer

    monkeypatch.setenv("STOA_PRIVATE", "1")
    monkeypatch.setattr(V, "_page_image", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rendered")))
    base = Answer(value=3, unit="count", confidence=0.3)
    assert V.read("/tmp/upload/x.png", "How many doors?", base=base) is None
    assert any("private mode" in t for t in base.trace)


def test_data_used():
    from stoa.agent.private import data_used

    assert data_used(["raster read it"], 0) == ["local"]
    assert data_used(["Gemini 3.1 Pro vision reading: 6"], 1) == ["local", "gemini"]
    assert data_used(["Nemotron 3.5 verifier: agrees"], 1) == ["local", "nvidia"]
