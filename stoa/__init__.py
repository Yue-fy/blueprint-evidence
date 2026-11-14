"""stoa: verifiable question answering over construction drawings (Junction 2026, Stoa challenge)."""

from stoa.contract import Answer, Evidence, Solver

try:  # T2: private mode (STOA_PRIVATE=1) refuses connections outside our machines in every stoa process
    from stoa import netguard as _netguard

    _netguard.install()
except Exception:  # noqa: BLE001 - the guard must never stop the package from importing
    pass

__all__ = ["Answer", "Evidence", "Solver"]
