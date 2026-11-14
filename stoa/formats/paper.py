"""Which paper sheet an image is, from its PIXEL size + the sheet size it prints (P1, 14 Nov 16:55).

Stoa's example PNGs carry no DPI tag (or PyMuPDF's 96), yet each is a standard sheet rendered at a usual export
resolution: 9934 x 7017 = A1 @ 300 dpi, 10200 x 6600 = ANSI D @ 300 dpi, 14000 x 9902 = A2 @ ~599 dpi. Pixels alone
are ambiguous by a factor 2 (ANSI D @ 300 = Tabloid @ 600; A2 @ 600 ~ A0 @ 300; A1 @ 300 = A3 @ 600), so a candidate
counts only when the sheet's own text names it ('ISO A1', 'A2', 'ANSI D'). Checked on the 4 Stoa sheets against their
PDF twins' page sizes: DfE A2 ✓, M-101A ANSI D ✓, S-100F A1 ✓; the AFCEC sheet (Tabloid @ 600 dpi) names no size ->
None (choosing 'the big sheet at 300 dpi' would have been wrong there).

    paper_from_pixels((w, h), texts) -> (dpi, 'A2') | None
"""

from __future__ import annotations

import re

# resolutions sheets are exported at; never 72 / 96 (screens: a screenshot is not a sheet)
EXPORT_DPI = (100, 150, 200, 240, 250, 300, 400, 600)
PIXEL_TOL = 0.004  # both sides within 0.4 % of the sheet at that resolution (Stoa's A2: 0.22 %)

PAPER_MM = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420), "A4": (210, 297), "A5": (148, 210),
            "Letter": (215.9, 279.4), "Legal": (215.9, 355.6), "Tabloid": (279.4, 431.8), "ANSI C": (431.8, 558.8),
            "ANSI D": (558.8, 863.6), "ANSI E": (863.6, 1117.6), "ARCH A": (228.6, 304.8), "ARCH B": (304.8, 457.2),
            "ARCH C": (457.2, 609.6), "ARCH D": (609.6, 914.4), "ARCH E": (914.4, 1219.2), "ARCH E1": (762, 1066.8)}
_ALIAS = {"Tabloid": ("tabloid", "ledger", "ansi b", r"11\s*[x×]\s*17"), "Letter": ("letter", "ansi a"),
          "Legal": ("legal",)}


def candidates(size: tuple[int, int]) -> list[tuple[str, int]]:
    """Every (sheet, export dpi) whose pixel size matches within PIXEL_TOL on both sides."""
    a, b = sorted(size)
    out = []
    for name, (sw, sl) in PAPER_MM.items():
        for d in EXPORT_DPI:
            ea, eb = sw / 25.4 * d, sl / 25.4 * d
            if abs(a - ea) <= PIXEL_TOL * ea and abs(b - eb) <= PIXEL_TOL * eb:
                out.append((name, d))
    return out


def _named(name: str, texts: list[str]) -> bool:
    """Does any text item name this sheet size? A-sizes: an item that is just 'A1' / 'ISO A1' / 'A1 size' / '@ A1'
    ('A1/S-513' and 'GRID A1' are references, not sheet sizes)."""
    if name.startswith("A") and len(name) == 2:
        rx = re.compile(rf"^\s*(?:iso\s*|@\s*|sheet\s*(?:size)?\s*:?\s*|paper\s*(?:size)?\s*:?\s*)?{name}"
                        rf"(?:\s*(?:size|sheet|paper))?\s*$|@\s*{name}\b|\biso\s*{name}\b", re.I)
    else:
        words = _ALIAS.get(name, (re.escape(name.lower()).replace(r"\ ", r"\s*"),))
        rx = re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)
    return any(rx.search(t or "") for t in texts)


def paper_from_pixels(size: tuple[int, int], texts: list[str]) -> tuple[float, str] | None:
    """(exact dpi, sheet) when exactly one pixel-size candidate is named by the sheet's text, else None. The dpi is
    computed from the sheet's size (14000 px over A2's 594 mm = 598.6 dpi), not the nominal export value."""
    names = {n for n, _ in candidates(size)}
    hit = [n for n in names if _named(n, texts)]
    if len(hit) != 1:
        return None
    sw, sl = PAPER_MM[hit[0]]
    a, b = sorted(size)
    return (a / (sw / 25.4) + b / (sl / 25.4)) / 2, hit[0]
