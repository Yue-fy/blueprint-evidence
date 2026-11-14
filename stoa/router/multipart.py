"""Multi-part questions whose parts ask DIFFERENT quantities or refer back ("How many rooms labelled 'Zimmer' does
apartment C have, and what is their total area?", "Wie viele Küchen gibt es und wie gross ist die grösste?", "What are
the net areas of apartment A and of apartment B?"). compose.py covers same-quantity lists ("how many doors and how
many windows"); these went to one solver that answered the first part only (red team multi32: 3 of 25 such misses).

The local text model splits the question into self-contained sub-questions (names and numbers verbatim, references
resolved), each part goes through the atomic cascade, and the answer is 'v1 ; v2' with every part's evidence.
"""

from __future__ import annotations

import re

# a second question clause: ", and what/how/which ..." in the languages the rules cover
_SECOND_Q = re.compile(
    r"(?:,|\s)\s*(?:and|und|et|e|ja|och|en|y|og)\s+(?:also\s+)?"
    r"(?:what|how|which|where|wie|was|welche\w*|wo|quel\w*|combien|comment|qual\w*|quant\w*|come|dove|"
    r"mik\w*|mit\w*|kuinka|monta|hur|vad|vilk\w*|hoe|wat|welk\w*|hvor\w*|cu[aá]nt\w*|qu[eé])\b", re.I)
# "of apartment A and of apartment B" / "von Wohnung A und von Wohnung B"
_OF_AND_OF = re.compile(r"\b(of|von|de|di|för)\s+(?:the\s+)?(\w+)\s+\S+\s+(?:and|und|et|e|och)\s+(?:of|von|de|di|för)\b",
                        re.I)
_TOTAL_CUE = re.compile(r"\b(?:in total|total|altogether|together|combined|sum|insgesamt|zusammen|au total|in tutto|"
                        r"yhteensä|totalt|respectively)\b", re.I)

SPLIT_SCHEMA = {"type": "object", "properties": {
    "parts": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "maxLength": 300}}},
    "required": ["parts"]}
SPLIT_PROMPT = """Split this question about a building drawing into its separate sub-questions, in English.
Each sub-question must be complete on its own: repeat the names, room labels, apartment letters, storey names and
numbers exactly as written (keep quotes), and replace words like "their", "it", "the largest one" with what they
refer to. Keep the question's scope (this floor plan, apartment X, storey Y) in every part.
If it asks only ONE thing, return just that one question.
QUESTION: {q}
Reply with JSON only: {{"parts": ["...", "..."]}}"""


_QUANTITY = re.compile(r"\bhow (?:many|much|big|large|long|wide|high)\b|\b(?:areas?|sizes?|lengths?|widths?|heights?|volumes?|m²|m2|"
                       r"wie (?:viele?|gro(?:ss|ß)|lang|breit|hoch)|fläche|combien|surface|quant\w*|superficie|montako|"
                       r"kuinka (?:monta|suuri|pitkä)|pinta-ala|hur (?:många|stor|lång)|hoeveel|hoe groot|oppervlakte)\b",
                       re.I)
# IFC splits its own parts (W1: '46 (… ground floor); 39'); ifc_bench is W1's free-text fact-card path; schedule
# sheets ask text pairs ('what door type …, and what kind of door is that?') that are one answer
_SKIP_SOURCES = {"ifc_models", "ifc_bench", "opening_design"}


def is_multipart(question: str, source: str = "") -> bool:
    q = question.strip()
    if source in _SKIP_SOURCES or not _QUANTITY.search(q):
        return False
    # 'the total net area of apartment A and of apartment B' is ONE sum; '…, and what is their total area?' is a part
    return bool(_SECOND_Q.search(q) or (_OF_AND_OF.search(q) and not _TOTAL_CUE.search(q)))


def split(question: str) -> tuple[list[str], object]:
    """(parts, ChatResult or None). A single part / failure -> []."""
    from stoa import llm

    try:
        r = llm.chat(SPLIT_PROMPT.format(q=question), backend=llm.text_backend(), max_tokens=400, temperature=0.0,
                     json_schema=SPLIT_SCHEMA)
    except Exception:  # noqa: BLE001
        return [], None
    d = llm.extract_json(r.text) if not r.text.strip().startswith("{") else None
    if d is None:
        import json

        try:
            d = json.loads(r.text)
        except Exception:  # noqa: BLE001
            d = {}
    parts = [str(p).strip() for p in (d or {}).get("parts", []) if str(p).strip()]
    parts = list(dict.fromkeys(parts))
    return (parts if 2 <= len(parts) <= 4 else []), r


__all__ = ["is_multipart", "split"]
