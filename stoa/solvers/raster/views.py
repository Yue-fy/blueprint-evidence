"""Which printed view of a sheet a text item belongs to (O1 c11).

US module sets print every room twice: in the '… - FLOOR PLAN' and in the '… - RCP' (reflected ceiling plan),
side by side, each with its view title printed BELOW the view. The RCP carries ceiling-height tags (9'-0") right next
to the room's tag, so a width read off the texts can cite a ceiling height (X1 21:32, AFCEE A-101 scan: 'How wide is
the opening into the Break Room 2.02?' -> 9'-0" @0.75; gold 4'-8"). A view's title is found as a short, larger-than-
usual line with a view word; an item belongs to the title below it that is closest (vertical gap + horizontal gap to
the title's span).
"""
from __future__ import annotations

import os
import re

from . import textlayer as T

GUARD = os.environ.get("STOA_O1_RCP_GUARD", "1") != "0"

RCP_TITLE = re.compile(r"(?<![A-Za-z])R\.?\s?C\.?\s?P\.?(?![A-Za-z])|reflected\s+ceiling|ceiling\s+plan|"
                       r"alakattopiir\w*|alakattokuva|deckenspiegel|deckenuntersicht|plan\s+de\s+plafond|"
                       r"innertak(?:s)?plan|controsoffitt\w*", re.I)
_VIEW = re.compile(r"\b(?:plans?|elevations?|sections?|details?)\b|pohjapiir\w*|grundriss|leikkaus|julkisivu|"
                   r"schnitt|ansicht|planritning", re.I)
_FLOOR = re.compile(r"\bplan\b|pohjapiir\w*|grundriss|planritning", re.I)
_NOT_FLOOR = re.compile(r"\b(?:roof|site|key|location|plot|framing|foundation|ceiling|reflected)\s+plan", re.I)
_NOTE = re.compile(r"^\s*(?:see|refer|ref\.?|note|notes|per|as|typ\.?|similar)\b", re.I)
# the question asks about a ceiling / a height: the RCP is the right view for it
CEIL_Q = re.compile(r"\b(?:ceilings?|clg|soffits?|headroom|height|high|tall|alakat\w*|decke\w*|plafond\w*|"
                    r"soffitt\w*|innertak\w*|höhe|hoch|korkeu\w*|h[öo]jd\w*|hauteur|altezza)\b", re.I)


def titles(tl: T.TextLayer, max_words: int = 8) -> list[tuple[str, T.TextItem]]:
    """[(kind, item)] for the view titles: 'rcp', 'floor' or 'other'. A title is short (<= max_words words), printed
    at least 1.2x the median text height, and is not a note ('SEE RCP FOR CEILING HEIGHTS')."""
    hs = sorted(i.box[3] - i.box[1] for i in tl.items if i.box[3] > i.box[1])
    if not hs:
        return []
    med = hs[len(hs) // 2]
    out = []
    for it in tl.items:
        t = it.text.strip()
        if not t or len(t.split()) > max_words or _NOTE.match(t) or (it.box[3] - it.box[1]) < 1.2 * med:
            continue
        if RCP_TITLE.search(t):
            out.append(("rcp", it))
        elif _FLOOR.search(t) and not _NOT_FLOOR.search(t):
            out.append(("floor", it))
        elif _VIEW.search(t):
            out.append(("other", it))
    return out


def view_of(it: T.TextItem, tits: list[tuple[str, T.TextItem]]) -> str | None:
    """The kind of the view title closest below `it` (vertical gap + horizontal gap to the title's span); None when
    no title lies below it within 1.2 title widths sideways (title block, notes)."""
    best = None
    for kind, t in tits:
        if t is it:
            return kind
        dy = t.box[1] - it.box[3]
        if dy < 0:
            continue
        dx = max(0.0, t.box[0] - it.cx, it.cx - t.box[2])
        if dx > 1.2 * (t.box[2] - t.box[0]):  # far beside every title (title block, notes): no view
            continue
        s = dy + dx
        if best is None or s < best[0]:
            best = (s, kind)
    return best[1] if best else None


def rcp_cited(question: str, cited: list, tl: T.TextLayer) -> str | None:
    """A note when every value-bearing cited line of a size answer lies in a reflected ceiling plan view, the sheet
    also prints a floor plan view, and the question is not about a ceiling or a height; else None."""
    if not GUARD or not cited or CEIL_Q.search(question):
        return None
    vals = [i for i in cited if re.search(r"\d", i.text) and not T.parse_scales(i.text)]
    if not vals:
        return None
    tits = titles(tl)
    if not any(k == "rcp" for k, _ in tits) or not any(k == "floor" for k, _ in tits):
        return None
    if all(view_of(i, tits) == "rcp" for i in vals):
        names = ", ".join(f"'{i.text[:20]}'" for i in vals[:3])
        return (f"the cited {names} {'is' if len(vals) == 1 else 'are'} printed in the reflected ceiling plan (RCP: "
                "ceiling-height tags), not in the floor plan, so it is no dimension of the asked element")
    return None
