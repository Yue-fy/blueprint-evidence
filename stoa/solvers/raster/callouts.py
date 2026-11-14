"""Slab callouts on structural plans: '150 SLAB-ON-GRADE W/ D13 @ 300', '200 THK SLAB', 'SLAB 150 THK', '6" CONC. SLAB'.

A thickness question about a slab is answered from the callout block whose text carries the question's own
distinguishing token (the bar 'D13', '#4', the type tag 'S2'); the number after '@' in that block is the bar SPACING,
never the thickness (X1 stoa-like S-100F PNG sl-054: 'How thick is the slab reinforced with D13 bars?' -> 300 mm
@0.75 from 'D13 @ 300'; the callout above it says 150).

    slab_callouts(tl) -> [Callout(value, unit, item, block_text, block_items)]
    thickness_answer(img_path, question, tl, solver) -> Answer | None
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from stoa.contract import Answer, Evidence

CONF = 0.7  # one callout block carries the question's token (or every callout prints the same thickness)
SAME_CONF = 0.6  # no token in the question; all callouts agree

_THICK_Q = re.compile(r"\b(thick\w*|dick\w*|stärke|paksu\w*|épaisseur|spessore|espesor|tjock\w*)\b", re.I)
_SLAB_Q = re.compile(r"\b(slabs?|plates?|platte|decke|laatta\w*|dalles?|solett\w*|losa|deck|topping|bodenplatte|"
                     r"slab-on-grade|floor slab)\b", re.I)
_SLAB = r"(?:CONC(?:RETE)?\.?\s*)?(?:SLAB|S\.O\.G\.?|SOG|TOPPING|DECK)"
_CALLOUT = [
    # '150 SLAB-ON-GRADE', '200 THK SLAB', '200mm THK. CONCRETE SLAB'
    re.compile(rf"^\s*(\d{{2,3}})\s*(mm)?\s*(?:THK\.?|THICK)?\s*{_SLAB}\b", re.I),
    # 'SLAB 150 THK', 'SLAB THICKNESS 200 mm', 'SLAB: 180mm'
    re.compile(rf"\b{_SLAB}\s*(?:THK\.?|THICKNESS|THICK)?\s*[:=]?\s*(\d{{2,3}})\s*(mm|THK)\b", re.I),
    # '6" CONC. SLAB', '5 1/2" SLAB ON GRADE'
    re.compile(rf"^\s*(\d{{1,2}}(?:\s+\d/\d)?)\s*(\"|”|″|IN\.?)\s*(?:THK\.?|THICK)?\s*{_SLAB}\b", re.I),
]
_TOKEN = re.compile(r"\b(?:[A-Z]{1,3}-?\d{1,3}|#\d{1,2})\b")  # D13, D16, #4, S2, SL-1 (bar sizes, type tags)
_SPACING = re.compile(r"@\s*(\d{2,4})")


@dataclass
class Callout:
    value: float  # mm or inches
    unit: str  # 'mm' | 'in'
    item: object
    block_items: list = field(default_factory=list)

    @property
    def block_text(self) -> str:
        return " ".join(i.text for i in self.block_items)


def _num(s: str) -> float:
    s = s.strip()
    m = re.match(r"(\d+)\s+(\d)/(\d)", s)
    return int(m.group(1)) + int(m.group(2)) / int(m.group(3)) if m else float(s)


def _block(tl, item) -> list:
    """The callout's text block: lines starting at or below it within 3 line heights and overlapping it in x."""
    x0, y0, x1, y1 = item.box
    h = max(4.0, y1 - y0)
    out = [item]
    for i in tl.items:
        if i is item:
            continue
        a0, b0, a1, b1 = i.box
        if y0 - 0.5 * h <= b0 <= y1 + 3 * h and a0 <= x1 + h and a1 >= x0 - h:
            out.append(i)
    return sorted(out, key=lambda i: (i.box[1], i.box[0]))


def slab_callouts(tl) -> list[Callout]:
    out = []
    for i in tl.items:
        for rx in _CALLOUT:
            m = rx.search(i.text)
            if not m:
                continue
            try:
                v = _num(m.group(1))
            except ValueError:
                continue
            u = "in" if (m.group(2) or "").strip().upper() in ('"', "”", "″", "IN", "IN.") else "mm"
            if u == "mm" and not 50 <= v <= 600 or u == "in" and not 2 <= v <= 24:
                continue
            out.append(Callout(value=v, unit=u, item=i, block_items=_block(tl, i)))
            break
    return out


def thickness_answer(img_path: str, question: str, tl, solver: str = "raster") -> Answer | None:
    if os.environ.get("STOA_O1_CALLOUTS", "1") == "0":
        return None
    if not (_THICK_Q.search(question) and _SLAB_Q.search(question)):
        return None
    cs = slab_callouts(tl)
    if not cs:
        return None
    toks = {t.upper().replace("-", "") for t in _TOKEN.findall(question.upper())}
    picked = [c for c in cs if toks and toks & {t.upper().replace("-", "") for t in _TOKEN.findall(c.block_text.upper())}]
    # a thickness the question itself states ('the 300 mm slab with D16 bars') does not pick a block by itself
    note = ""
    if picked:
        vals = {(c.value, c.unit) for c in picked}
        conf = CONF
        note = f"the callout carrying {', '.join(sorted(toks))} from the question"
    else:
        vals = {(c.value, c.unit) for c in cs}
        conf = SAME_CONF
        note = "every slab callout on the sheet prints this thickness"
        if toks:
            return None  # the question names a bar / type that no callout block shows: another reader
    if len(vals) != 1:
        return None
    v, u = vals.pop()
    use = picked or cs
    c = use[0]
    trace = [f"slab callout '{c.block_text[:90]}': thickness {v:g} {u} ({note})"]
    sp = _SPACING.search(c.block_text)
    if sp:
        trace.append(f"'@ {sp.group(1)}' in the same callout is the bar spacing, not the thickness")
    metric = f" = {v * 25.4:.0f} mm" if u == "in" else f" = {v / 1000:g} m"
    trace.append(f"Convention: slab thickness as printed in the slab callout ({v:g} {u}{metric})")
    ev = [Evidence(file=img_path, page=0, bbox=[round(x, 1) for x in i.box], bbox_space="image_px",
                   note=f"slab callout text '{i.text[:60]}'") for i in c.block_items[:4]]
    return Answer(value=v, unit=u, confidence=conf, solver=solver, trace=trace, evidence=ev,
                  assumptions=["the leading number of a slab callout ('150 SLAB-ON-GRADE …') is the slab thickness"])
