"""Is a PP-OCR reading garbage? (O1 c8, Sat 14 Nov).

On a heavily degraded scan PP-OCR returns word-like garbage for room labels ('Faress Roons' = Fitness Room,
'Prant Poom', 'Secondiry Chatsroom'): X1 19:16-19:42 measured the DfE sheet as a 200-dpi scan at 1/9 questions with
the 'auto' engine (PP-OCR) and 7/9 with the VLM text layer, which read 60/99 labels exactly where PP-OCR read 3.

Signal: the share of alphabetic OCR tokens (>= 4 letters) that are dictionary words. Over the 376 cached OCR layers
with >= 30 such tokens: X1's heavy DfE scans 0.42-0.48 (mean recogniser score 0.88), lighter EFA scans 0.67-0.71,
HABS / clean renders 0.82-0.98. Foreign-language sheets score low too (Swiss German 0.25-0.53, Finnish archive
sheets 0.00-0.08) but read with recogniser scores 0.94-0.99 (Swiss) or sit below the English floor (Finnish), so the
trigger is: >= MIN_TOKENS tokens, LOW <= share < HIGH and mean recogniser score < MAX_REC. A trigger only asks for a
second reading: the caller keeps the VLM layer when its own share beats the OCR's by MARGIN, else the OCR layer
(a false trigger costs latency, not accuracy).

The English list is /usr/share/dict/words (SCOWL, present on our server); without it the gate is off (None).
STOA_O1_OCR_GATE=0 turns it off.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DICT_FILES = ("/usr/share/dict/words", "/usr/share/dict/american-english", "/usr/share/dict/british-english")
MIN_TOKENS = 30
LOW, HIGH = 0.2, 0.6  # below LOW: another language (or nothing readable either way) -> keep the OCR
MAX_REC = 0.93  # mean PP-OCR recogniser score; Swiss German sheets read at 0.94-0.99, the heavy DfE scans at 0.85-0.88
MARGIN = 0.1  # the VLM layer must beat the OCR's dictionary share by this much to replace it

# drawing words / abbreviations not in the English list, and a few room words of the languages in our data
DOMAIN = {
    "corr", "stor", "mech", "elec", "bldg", "dwgs", "conc", "reinf", "ctrl", "galv", "insul", "lino", "cupb", "wardr",
    "ensuite", "wc's", "kitchenette", "toilets", "lobbies", "plantroom", "storeroom", "classbase", "dining",
    "keittiö", "olohuone", "makuuhuone", "kylpyhuone", "eteinen", "sauna", "parveke", "vaatehuone", "alkovi", "porras",
    "wohnzimmer", "küche", "schlafzimmer", "flur", "zimmer", "keller", "treppe", "balkon", "abstellraum", "diele",
    "wohnen", "essen", "kochen", "schlafen", "korridor", "nasszelle", "reduit", "treppenhaus", "estrich", "loggia",
    "waschküche", "gartensitzplatz", "dusche", "badezimmer", "kinderzimmer", "arbeitszimmer", "garderobe", "eingang",
    "vorplatz", "sitzplatz", "terrasse", "wohnung", "geschoss", "grundriss", "erdgeschoss", "obergeschoss", "projekt",
    "kök", "sovrum", "vardagsrum", "badrum", "hall", "förråd", "köök", "magamistuba", "elutuba", "vannituba",
    "cuisine", "salon", "chambre", "séjour", "cocina", "dormitorio", "baño", "sala", "cucina", "camera", "bagno",
}
_TOK = re.compile(r"[^\W\d_]{4,}")
_words: set[str] | None = None
_loaded = False


def words() -> set[str] | None:
    global _words, _loaded
    if not _loaded:
        _loaded = True
        for f in DICT_FILES:
            p = Path(f)
            if p.exists():
                try:
                    _words = {w.strip().lower() for w in p.read_text(errors="ignore").split() if w.strip().isalpha()}
                    _words |= DOMAIN
                    break
                except OSError:
                    continue
    return _words


def dict_share(texts) -> tuple[float, int] | None:
    """(share of tokens >= 4 letters that are words, number of tokens); None without a word list."""
    W = words()
    if W is None:
        return None
    toks = [t.lower() for s in texts for t in _TOK.findall(str(s))]
    if not toks:
        return 1.0, 0
    return sum(t in W for t in toks) / len(toks), len(toks)


@dataclass
class Verdict:
    share: float
    tokens: int
    rec: float
    garbled: bool

    def describe(self) -> str:
        return (f"OCR dictionary-word share {self.share:.2f} of {self.tokens} tokens, mean recogniser score "
                f"{self.rec:.2f}")


def judge(items: list[dict]) -> Verdict | None:
    """items: the OCR reading's {'text', 'score'} dicts (stoa.ocr.read)."""
    if os.environ.get("STOA_O1_OCR_GATE", "1") == "0":
        return None
    r = dict_share(i.get("text", "") for i in items)
    if r is None:
        return None
    share, n = r
    rec = sum(float(i.get("score", 1.0)) for i in items) / len(items) if items else 1.0
    return Verdict(round(share, 3), n, round(rec, 3), n >= MIN_TOKENS and LOW <= share < HIGH and rec < MAX_REC)
