"""V1: the 'verda' text backend (Verda Inference) is configured and refuses images without any network call."""
import pytest

from stoa import llm


def test_verda_backend_config():
    cfg = llm.BACKENDS["verda"]
    assert cfg["api_key_env"] == "VERDA_INFERENCE_API_KEY"
    assert cfg["base_url"].startswith("https://")
    assert not llm.is_local("verda")  # billed per call, not in GPU seconds


def test_verda_is_text_only():
    from PIL import Image

    with pytest.raises(ValueError, match="text only"):
        llm.chat("how many doors?", images=[Image.new("RGB", (8, 8))], backend="verda")


def test_text_backend_accepts_verda_name(monkeypatch):
    monkeypatch.setenv("STOA_TEXT_BACKEND", "verda")
    # without a reachable endpoint text_backend() falls back to the default instead of raising
    monkeypatch.setattr(llm, "_client", lambda b: (_ for _ in ()).throw(RuntimeError("offline")))
    llm._TB_SEEN.pop("verda", None)
    assert llm.text_backend() == "local"
