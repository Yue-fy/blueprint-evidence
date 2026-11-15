"""What kind of drawing is this sheet? (floor plan, site plan, elevation, section, detail, map, ...)

A question about rooms ("How many bedrooms are shown on this floor plan?") asked on an ELEVATION has no answer on that
sheet; the counting models still return a number (red team, Q 05:04: HABS north elevation -> CubiCasa LoRA "0" @ 0.75).
This module tells the caller which views a raster sheet contains, from two readers:

    title_kinds(layer)          -> view words printed on the sheet ('NORTH ELEVATION', 'POHJA 1/100', 'SECTION A-A'),
                                   with boxes; free when the text layer is cached (artifacts/W3/textlayer)
    vlm_kinds(path)             -> one small VLM call on the downscaled sheet (~1k tokens): views + main view
    sheet_kind(path)            -> both combined: {views, main, has_plan, title, source}
    veto(path, question)        -> None, or a note + evidence when the question needs a floor plan and the sheet
                                   has none (the caller caps its confidence and states it in `missing`)

Measured on 19 hand-labelled scans (artifacts/W3/sheet_kind_gold.jsonl, written by eye before any run): see
experiments/w3/sheet_kind_eval.py and KNOWLEDGE.md (W3, 05:4x).
"""

from __future__ import annotations

import re
from pathlib import Path

from stoa import llm
from stoa.contract import Evidence

from . import textlayer as T

KINDS = ["floor_plan", "site_plan", "elevation", "section", "detail", "perspective", "map", "photo", "other"]

# view words as printed in titles (EN / FI / SV / DE / FR / IT / ES); order matters: site plan before plan
_PAT = {
    "site_plan": r"site\s*plan|plot\s*plan|location\s*plan|asemapiir|asemakaava|tonttikartta|situationsplan|"
                 r"lageplan|plan\s+de\s+situation|plan\s+masse|planimetria",
    "floor_plan": r"(?:floor|ground|first|second|third|fourth|attic|basement|cellar|main|upper|lower|level|storey|"
                  r"story|\d+(?:st|nd|rd|th))\s+(?:floor\s+|level\s+|story\s+)?plan\b|floor\s*plan|\bplan\s+(?:af|över|du|de)\b|"
                  r"\bplan\s+of\s+(?:the\s+)?(?:\w+\s+)?(?:floor|stor[e]?y|level|basement|cellar|attic|ground)|pohjapiir|\bpohja\b|kerros|kellari|grundriss|geschoss|planritning|våningsplan|"
                  r"\bpianta\b|\bplanta\b|rez-de-chauss|\bétage",
    "elevation": r"\belevations?\b|julkisivu|\bfasad|fa[cç]ade|ansicht|prospett|alzado|aufriss",
    "section": r"\bsections?\b|leikkaus|\bschnitt|\bcoupe\b|sezione|sektion|\bsnitt\b|secci[oó]n|\bcorte\b",
    "detail": r"\bdetails?\b|yksityiskoh|detalj|d[ée]tail|dettaglio",
}
_BARE_PLAN = re.compile(r"\bplan\b", re.I)  # 'PLAN A-A' of a column, 'PLAN OF PINTLE': often a detail view
_SEP = re.compile(r"[·•∙_]+")
_NOT_FLOOR = re.compile(r"\b(?:key|roof|framing|foundation|site|plot|location|ceiling|reflected)\s+plan", re.I)

# questions that need a floor plan: rooms and room fixtures (windows/doors/storeys can be read on elevations)
ROOM_Q = re.compile(
    r"\b(?:bed\s*rooms?|bath\s*rooms?|kitchens?|rooms?|toilets?|wcs?|w\.c\.|sauna|saunas|closets?|wardrobes?|"
    r"hall(?:way)?s?|corridors?|living\s*rooms?|dining\s*rooms?|offices?|floor\s*area|floor\s*space|"
    r"makuuhuone\w*|huone\w*|keitti\w*|kylpyhuone\w*|kph|eteis\w*|olohuone\w*|"
    r"sovrum\w*|kök\w*|badrum\w*|\brum\b|"
    r"zimmer\w*|schlafzimmer\w*|küche\w*|bad(?:ezimmer)?|räume?|"
    r"chambres?|cuisines?|salles?\s+de\s+bains?|pièces?|"
    r"camer[ae]|cucin[ae]|bagn[io]|stanz[ae]|locali)\b", re.I)
_PLAN_Q = re.compile(r"floor\s*plan|pohjapiir|grundriss|plan\b", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "views": {"type": "array", "items": {"type": "string", "enum": KINDS}},
        "main": {"type": "string", "enum": KINDS},
        "rooms_visible": {"type": "boolean"},
    },
    "required": ["views", "main", "rooms_visible"],
}
PROMPT = ("This image is one sheet of an architectural drawing set (possibly a historical scan). Which kinds of views "
          "does the sheet contain? floor_plan = a building seen from above, cut through, showing its rooms and walls; "
          "site_plan = buildings as footprints on a plot or map; elevation = the outside of a building seen from the "
          "side (facade); section = a building cut vertically; detail = an enlarged part (window, door, molding, "
          "joint); perspective = a 3D view; map; photo; other. List every kind present in `views`, the largest one "
          "in `main`, and whether individual rooms of a building can be seen from above (`rooms_visible`). JSON only.")


def title_kinds(layer: T.TextLayer, max_words: int = 8) -> list[dict]:
    """View words printed on the sheet. A hit is a 'title' when its line is short (<= max_words words): long lines
    are notes ('SEE PLAN ON SHEET 3'). Sorted: titles first, then by text height (bigger print = sheet title)."""
    out = []
    for it in layer.items:
        t = _SEP.sub(" ", it.text)  # 'PLAN·AF·BOSTADS·VANINGEN.' (Helsinki archive sheets)
        for k, p in _PAT.items():
            m = re.search(p, t, re.I)
            if not m:
                continue
            if k == "floor_plan" and (_NOT_FLOOR.search(t) or re.search(_PAT["site_plan"], t, re.I)):
                continue
            out.append({"kind": k, "text": t, "box": it.box, "title": len(t.split()) <= max_words,
                        "height": round(it.box[3] - it.box[1], 1)})
    return sorted(out, key=lambda h: (not h["title"], -h["height"]))


def vlm_kinds(path: str, backend: str = "local", max_side: int = 1024) -> dict | None:
    """One small VLM call on the downscaled sheet -> {views, main, rooms_visible} (None on failure)."""
    try:
        r = llm.chat(PROMPT, images=[str(T._abspath(path))], backend=backend, max_tokens=120, json_schema=SCHEMA,
                     max_pixels=max_side * max_side, max_side=max_side, cache=True)
        import json

        d = json.loads(r.text)
        d["views"] = [v for v in d.get("views", []) if v in KINDS] or [d.get("main", "other")]
        return d
    except Exception:  # noqa: BLE001 - a failed read is 'unknown', never a veto
        return None


def cached_layer(path: str) -> T.TextLayer | None:
    """The text layer if it was read before (memory or disk); never triggers a 20-60 s cold read."""
    import os

    eng = os.environ.get("STOA_TEXT_ENGINE", "auto").lower()
    if eng in ("ocr", "auto"):  # O1: an OCR-read layer counts too (auto: only where text_layer would use it)
        from stoa import ocr

        okey = f"{path}{T.file_tag(path)}|ocr"
        if f"{okey}|gated" in T._mem:
            return T._mem[f"{okey}|gated"]
        if okey in T._mem:
            return T._mem[okey]
        if ocr.cached(path) and (eng == "ocr" or T.ocr_suits(path)):
            if T.gate_pending(path):  # garbled OCR, VLM layer not read yet: the OCR reading, no cold VLM read
                return ocr.text_layer(path)
            return T.text_layer(path)
    key = f"{path}{T.file_tag(path)}|local|1100"  # = text_layer's memory key
    if key in T._mem:
        return T._mem[key]
    if T._disk_key(path, 1100, "local").exists():
        if eng in ("ocr", "auto") and os.environ.get("STOA_O1_CACHED_VLM", "1") != "0":
            from stoa import ocr

            if not ocr.cached(path) and (eng == "ocr" or T.ocr_suits(path)):
                # O1 c11: text_layer would start a cold OCR read here (up to its 120 s budget, and a sticky VLM note
                # when it runs over): the cached VLM reading is what this function promises
                return T._vlm_layer(path, "local", 1100, 2000, 2500, 6)
        return T.text_layer(path)  # disk hit: billed as computed, no LLM call
    return None


def sheet_kind(path: str, use_vlm: bool = True, layer: T.TextLayer | None = None) -> dict:
    """{views, main, has_plan, title, source}. has_plan: True / False / None (unknown).
    Printed titles first (free with a cached text layer): a floor-plan title -> True; titles that name only other views
    and no 'plan' word at all -> False. Otherwise one small VLM call decides. 19 hand-labelled scans: text decides 13,
    right 13; VLM alone 16/19; text-then-VLM 16/19; no plan sheet was ever called plan-less (0/8 false vetoes). The
    3 misses are site plans of ruins the VLM calls 'rooms visible' (no veto there: the safe side)."""
    layer = layer or cached_layer(path)
    hits = title_kinds(layer) if layer else []
    titles = [h for h in hits if h["title"]]
    out = {"views": sorted({h["kind"] for h in titles}), "main": titles[0]["kind"] if titles else None,
           "has_plan": None, "title": titles[0] if titles else None, "source": "text" if titles else "none"}
    if any(h["kind"] == "floor_plan" for h in titles):
        out["has_plan"] = True
        out["main"], out["title"] = "floor_plan", next(h for h in titles if h["kind"] == "floor_plan")
    elif titles and not any(_BARE_PLAN.search(h["text"]) for h in hits) and not any(
            len(i.text.split()) <= 8 and _BARE_PLAN.search(_SEP.sub(" ", i.text)) for i in layer.items):
        # O1 c8: a 'plan' word on ANY short line defers to the VLM ('-PLAN' under a Helsinki archive plan view)
        rooms = room_labels(layer)
        if len(rooms) >= ROOM_LABELS_UNSURE:
            # O1 c8: HABS 215 Abingdon Ave titles only its elevations but prints a plan with 'LIVING ROOM', 'KITCHEN?',
            # 'BEDROOM?' (and their sizes): the text veto capped 3 right areas at 0.05. Room names can also label a
            # section, so the text no longer decides: unknown -> the VLM look below
            out["rooms_printed"] = rooms[:6]
        else:
            out["has_plan"] = False
    if out["has_plan"] is None and use_vlm:
        v = vlm_kinds(path)
        if v:
            out.update(vlm=v, source="text+vlm" if titles else "vlm", main=v["main"],
                       views=sorted(set(out["views"]) | set(v["views"])),
                       has_plan="floor_plan" in v["views"] or bool(v.get("rooms_visible")))
            same = [h for h in titles if h["kind"] == v["main"]]
            out["title"] = same[0] if same else out["title"]
    return out


ROOM_LABELS_UNSURE = 3
_ROOM_WORD = re.compile(r"^\W*(?:[^\W\d_]+\s+){0,2}(?:rooms?|bed\s?rooms?|kitchens?|bath(?:rooms?)?|living|dining|halls?|lobby|"
                        r"corridor|office|toilets?|wc|closet|stor(?:e|age)|porch|parlou?r|classroom|laundry|pantry|garage|"
                        r"study|foyer|vestibule|olohuone|makuuhuone|keittiö|kylpyhuone|eteinen|wohnen|zimmer|küche|kök|"
                        r"sovrum|chambre|cuisine|salon|cocina|dormitorio)\b[\s?.!]*(?:\d{1,4}[A-Za-z]?)?\W*$", re.I)


def room_labels(layer) -> list[str]:
    """Distinct printed room names ('LIVING ROOM', 'KITCHEN?', 'Bedroom 2') on a text layer."""
    if layer is None:
        return []
    seen = []
    for i in layer.items:
        t = str(i.text).strip()
        if len(t) <= 30 and _ROOM_WORD.match(t) and t.lower() not in (x.lower() for x in seen):
            seen.append(t)
    return seen


_ASK = re.compile(r"how\s+many|number\s+of|\bcount|\bareas?\b|\bsize\b|square|m2|m²|sq\.?\s*ft|largest|smallest|biggest|"
                  r"which\s+room|montako|kuinka|lukumäärä|pinta-ala|wie\s+viele|anzahl|fläche|combien|nombre\s+de|"
                  r"surface|quant[ie]|numero\s+di|superficie|hur\s+många|antal|yta|cuántos|cuántas", re.I)


def needs_plan(question: str) -> bool:
    """A count / size question about rooms ('How many bedrooms...', 'area of the kitchen'); a name or date question
    that happens to contain 'hall' is not one."""
    return bool(ROOM_Q.search(question) and _ASK.search(question))


def veto(path: str, question: str, use_vlm: bool = True) -> dict | None:
    """None when the sheet may answer the question; else {note, missing, evidence, kind} for a question about rooms
    on a sheet that shows no floor plan. Only raster sheets (PNG/JPG/TIF) of a source other than cubicasa5k/swiss."""
    if Path(path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        return None
    if "cubicasa5k" in path or "swiss_dwellings" in path or not needs_plan(question):
        return None
    k = sheet_kind(path, use_vlm=use_vlm)
    if k["has_plan"] is not False:
        return None
    main = (k["main"] or "other").replace("_", " ")
    t = k.get("title")
    shown = f" ('{t['text'][:40]}')" if t else ""
    ev = [Evidence(file=path, page=0, bbox=[round(x, 1) for x in t["box"]], bbox_space="image_px",
                   note=f"sheet title: '{t['text'][:60]}'")] if t else []
    return {"kind": k["main"], "views": k["views"], "source": k["source"], "evidence": ev,
            "note": f"this sheet is {'an' if main[0] in 'aeiou' else 'a'} {main}{shown}: it shows no floor plan, "
                    "so rooms cannot be counted or measured on it",
            "missing": f"a floor-plan sheet of this building (this one shows: {', '.join(k['views']) or main})"}
