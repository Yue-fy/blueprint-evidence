"""PRIVATE MODE (captain 18:53): 'drawings never leave our server'. W5's upload path sets STOA_PRIVATE=1 (and
STOA_EU_ONLY=1 for 'EU only', which still allows Verda's EU inference) for the call; the API / Pipeline flag private=1
does the same. Every hosted call site of stoa/agent (Gemini vision, the Gemini agent, the Nemotron verifier) asks
`hosted_allowed(backend)` first and behaves as if the service were unreachable when it says no."""

from __future__ import annotations

import contextvars
import os

_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar("stoa_private", default=False)  # Pipeline(private=1)
LOCAL = ("local", "local2", "default", "")


def private() -> bool:
    return _CTX.get() or os.environ.get("STOA_PRIVATE", "0") == "1"


def hosted_allowed(backend: str) -> bool:
    b = str(backend or "").lower()
    if b in LOCAL or b.startswith("local"):
        return True
    if not private():
        return True
    return b == "verda" and os.environ.get("STOA_EU_ONLY", "0") == "1"


class PrivateMode(RuntimeError):
    """Raised instead of a hosted call in private mode (callers treat it like an unreachable service)."""


def check(backend: str) -> None:
    if not hosted_allowed(backend):
        raise PrivateMode(f"private mode: no call to {backend} (the drawing stays on this server)")


def data_used(trace: list[str], api_calls: int) -> list[str]:
    """Where this answer's data went (captain 18:53; W5 reads Answer.data_used or the 'data_used: …' trace line):
    'local' always; hosted providers only when a hosted call (live or served from the cache) was made, named by the
    model credits the trace carries."""
    out = ["local"]
    if api_calls <= 0:
        return out
    text = " ".join(str(t) for t in trace or [])
    if "Gemini" in text:
        out.append("gemini")
    if "Nemotron" in text or "nvidia" in text.lower():
        out.append("nvidia")
    if os.environ.get("STOA_TEXT_BACKEND", "") == "verda" or len(out) == 1:
        out.append("verda" if os.environ.get("STOA_TEXT_BACKEND", "") == "verda" else "hosted")
    return out
