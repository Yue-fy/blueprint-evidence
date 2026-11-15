"""F6 Check list (captain 21:51, idea 4): rank every takeoff row and every answer on a sheet by how likely it is to be
wrong, so the engineer verifies only what needs them ('Check these 6 of 80').

Signals per item (the same code scores takeoff rows, the visitor's answers and the eval rows that calibrate it):
  confidence            the reader's own confidence (low < 0.5, fairly sure < 0.7)
  support               T1's evidence check (stoa.trust.support): 'supported' (a printed value / one mark per counted
                        item is marked), 'approximate', 'unlocated' (nothing on the drawing backs the number)
  agree                 two methods (F2's check: IFC space / traced outline vs printed tag; the pipeline's cross-check)
  trust colour          F6's trust map at the item's marks (red / amber / green)
  scale                 sizes only: the sheet's scale is not printed + confirmed (trust map scale status)
  poor sheet            the trust map's sheet warning
The weights are a logistic regression fitted on S0's d28 gate runs (upload path, Gemini off; 625 answers) and checked
on d29 (experiments/f6/checklist_cal.py; artifacts/F6/checklist_cal.txt). An item is flagged ('check this') when its
chance of being wrong is >= FLAG_AT, or F2 already put it in Review (no reading, a typical value, rough), or two
methods disagree. A person's tick ('checked') turns the row green and puts it back into the totals; flagged rows that
nobody has checked stay OUT of the totals (the 'Review' section, as the Finnish proof of concept does).

API (flag 'checklist'; uses F2's takeoff library directly, so the takeoff router need not be mounted):
  GET  /feat/checklist?path=&page=&visitor=        -> the check list of the sheet's finished takeoff, or its progress
  POST /feat/checklist/start {path, page}          -> starts (or finds) the takeoff (F2's takeoff.start)
  POST /feat/checklist/answers {path, page, answers, visitor} -> the visitor's own answers on this sheet, scored
  POST /feat/checklist/check {path, page, id, checked, visitor} -> a person's tick (kept per visitor + sheet)
Fails closed: any error -> {ok: false, error}; nothing else in the app changes.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

VERSION = "c2"

# ---- the model (experiments/f6/checklist_cal.py fits these on d28; FLAG_AT chosen there) ----------------------------
FEATURES = ("low", "mid", "unlocated", "approximate", "disagree", "agree", "red", "amber", "scale", "poor",
            "area", "length")
# fitted on d28 (548 answers, 174 wrong), rounded; 'agree' / 'disagree' are PRIORS, not fitted: the eval rows carry a
# two-method check only 8 times (d28) / 13 (d29: 11 right) and a disagreement never, while F2's takeoff rows carry one
# per room (IFC space / traced outline vs the printed tag). d29 AUC: fitted 0.847, confidence alone 0.827.
WEIGHTS = {"bias": -3.0, "low": 1.8, "mid": 1.2, "unlocated": 1.7, "approximate": 0.4, "disagree": 1.0,
           "agree": -0.5, "red": 0.7, "amber": 0.1, "scale": 0.6, "poor": 0.7, "area": 0.0, "length": -0.1}
FLAG_AT = 0.35  # d28: flagged 58 % wrong vs the rest 15 %, 71 % of the wrong answers flagged
# measured on d29 (held out; experiments/f6/checklist_cal.py): filled in by the calibration, shown in the panel
# ANSWERS (the visitor's own questions) are flagged by the reader's confidence alone (c < ANSWER_FLAG_CONF, or two methods
# disagree); the other signals stay as their REASONS. Measured (F6 01:5x, experiments/f6/checklist_conf.py + a threshold
# sweep): on every run confidence alone ranked wrong answers better than the 12-signal model (AUC d30 0.825 vs 0.809, d31
# 0.855 vs 0.844, T1's calibrated d31 0.871 vs 0.814), and the plain rule beats the model's flags at the same list size:
# d30 sl+wild 115 flagged = 58 % wrong / rest 12 % / 63 % caught (model 119: 51 / 14 / 57); d31 sl+wild 136 = 57 / 7.8 /
# 73 % (model 134: 51.5 / 9.9 / 65); T1-calibrated d31 sl 57 = 65 / 8.6 / 57 % (model 69: 45 / 10.9 / 48). Takeoff rows keep
# the model (F2's per-room confidence is coarse; its gold check is checklist_takeoff.py: 5 of 7 wrong rows caught).
ANSWER_FLAG_CONF = 0.5  # the reader's RAW confidence: flagged below it
# T1's CALIBRATED confidence (P(right); the answer's trace says 'calibration (T1): a -> b …', d32+): T1 01:58 'unsure' = <= 0.6
# (its doubt cap sets exactly 0.6), so the list agrees with the card. T1's d31 run (in-sample): <= 0.6 flags 96 of 381 =
# 49 % wrong, rest 6.3 %, 72 % of the wrong caught (< 0.5 there: 57 = 65 / 8.6 / 57 %); on RAW confidence <= 0.6 would list
# 199 of 381 (30 % wrong), hence two thresholds.
ANSWER_UNSURE_CALIBRATED = 0.6


def _calibrated(item: dict) -> bool:
    return any("calibration (T1)" in str(x) for x in (item.get("trace") or [])[:120])
CALIBRATION = {"split": "d30 gate (S0: stoa-like + wild dev, upload path, Gemini off), answers flagged by confidence < 0.5",
               "n": 441, "sheets": 53, "flagged": 115, "flagged_wrong": 67, "rest": 326, "rest_wrong": 40, "wrong": 107,
               "wrong_in_flagged": 67}  # d31 sl+wild (newest code): 136 = 57 % wrong, rest 7.8 %, 73 % caught.
#   Model era (answers ranked by the 12-signal model, shown until 01:5x): artifacts/F6/checklist_cal_t4_d30slwild.txt (shown: the
#   more conservative numbers; no scans in it, d30's scan lanes were killed by memguard). On it the ranking adds no lift over
#   the reader's confidence alone (AUC 0.809 vs 0.825): the list's worth is its reasons and building rules. A refit on d29
#   (checklist_cal_t4_fit29_test30.txt: 69 % caught, AUC 0.822) was NOT shipped: on the takeoff rows it grew the DfE L00
#   vector list 15 -> 80 of 258 and the scan's 133 -> 169 of 233 without catching more of the 7 wrong gold rows (5 either way).
#   d29 (with scans, held out from the d28 fit; checklist_cal_t4.txt): flagged 268 = 65 % wrong, rest 18 %, 71 % caught.

REASON = {
    "low": "low confidence",  # no raw numbers in the list (UX 23:30); the row's confidence stays in the JSON
    "mid": "only fairly sure",
    "unlocated": "nothing on the drawing backs this number",
    "approximate": "measured or estimated, not a printed value",
    "disagree": "two methods disagree",
    "red": "its mark sits in a hard-to-read (red) region",
    "amber": "its mark sits in a partly read (amber) region",
    "scale": "the scale is {scale}: sizes are estimates",
    "poor": "this sheet reads poorly",
}
# the same two reasons when the trust map itself is not switched on (the captain's demo subset has the check list but not
# the trust map): no colour the visitor could look for
REASON_NO_MAP = {"red": "its mark sits where the drawing is hard to read (scan noise, faint or broken lines)",
                 "amber": "its mark sits where the drawing is only partly readable"}
GOOD = {"agree": "two methods agree", "supported": "a printed value or one mark per item is marked"}


def _map_shown() -> bool:
    """True when the trust map is switched on for visitors (STOA_FEATURES names it, or 'all')."""
    try:
        from stoa.features import enabled

        return "trust_map" in enabled()
    except Exception:  # noqa: BLE001
        return False
MODEL_DIFF_PCT = 5.0  # F2's takeoff.MODEL_DIFF_PCT: a traced room further than this from its IfcSpace is 'model vs drawing differ' 


def kind_of(unit: str | None, question: str = "") -> str:
    u = (unit or "").lower().replace("²", "2")
    if u in ("count", "pcs", "no"):
        return "count"
    if u in ("m2", "ft2", "sqm", "sf", "sq ft", "m^2"):
        return "area"
    if u in ("m", "mm", "cm", "ft", "lf", "in"):
        return "length"
    if re.search(r"\bhow many\b|\bnumber of\b", question, re.I):
        return "count"
    return "other"


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def colour_at(tmap: dict | None, evidence: list[dict], page: int) -> str | None:
    """The trust colour under an item's marks: red when >= a third of its boxes sit in red, amber when half of them are
    not green (or its one box is amber), green otherwise; None when nothing is located on this page."""
    if not tmap:
        return None
    from stoa.features.trust_map import level_at

    levels = []
    for e in evidence or []:
        b = e.get("bbox")
        if not b or len(b) != 4 or (e.get("page") not in (None, page)):
            continue
        lv = level_at(tmap, [float(x) for x in b])
        if lv:
            levels.append(lv)
    if not levels:
        return None
    n = len(levels)
    red = sum(lv == "red" for lv in levels)
    if red * 3 >= n:
        return "red"
    if sum(lv != "green" for lv in levels) * 2 >= n:
        return "amber"
    return "green"


def agree_of(item: dict) -> bool | None:
    """Two-method agreement: F2's row check, else the pipeline's cross-check lines in the trace."""
    chk = item.get("check")
    if isinstance(chk, dict) and chk.get("agree") is not None:
        d = _num(chk.get("diff_pct"))
        if (not chk.get("agree") and d is not None and abs(d) <= MODEL_DIFF_PCT
                and "ifcspace" in str(item.get("method") or "").lower()):
            # the value IS the model's own room; only our cross-check trace is a few % off (F2 lists > 5 % as
            # 'drawing and model differ'): no signal either way (Esplanades floor 3: 22 such rows were force-listed)
            return None
        return bool(chk.get("agree"))
    comp = item.get("components")
    if isinstance(comp, dict) and comp.get("agree") is not None and item.get("quantity") == "area":
        return bool(comp.get("agree"))
    txt = " | ".join(str(t) for t in item.get("trace") or [])
    if re.search(r"two independent methods agree|cross-check[^|]*✓", txt):
        return True
    if re.search(r"cross-check[^|]*(?:✗|differ)|methods? (?:disagree|differ)|they differ by", txt):
        return False
    return None


def disagree_words(item: dict) -> str | None:
    """Which two readings differ, with both numbers (F2's row check): 'the IFC model says 63.82 m², the walls traced
    59.10 m² (−7.4 %)'. None when the row carries no check (the trace said they differ: the plain reason is used)."""
    chk = item.get("check")
    if not isinstance(chk, dict) or chk.get("agree") is not False:
        return None
    v, cv, d = _num(item.get("value")), _num(chk.get("value")), _num(chk.get("diff_pct"))
    u = UNIT.get(item.get("unit") or "", item.get("unit") or "")
    first = ("the IFC model says" if "ifcspace" in str(item.get("method") or "").lower() else
             "the printed value is" if "print" in str(item.get("method") or "").lower() else "one method gives")
    if v is None:
        return None

    def q(x: float) -> str:
        return f"{x:g} {u}".strip()

    if cv is None:
        return f"{first} {q(v)}, but the walls could not be traced to confirm it"
    pct = f" ({d:+.1f} %)" if d is not None else ""
    if first == "one method gives":
        return f"two methods disagree: {q(v)} vs {q(cv)}{pct}"
    how = str(chk.get("how") or "a second method")
    how = "the walls traced" if how.startswith("traced from the wall") else how
    return f"{first} {q(v)}, {how} {q(cv)}{pct}"


def support_of(item: dict, question: str) -> tuple[str | None, str]:
    """T1's support state ('supported' | 'approximate' | 'unlocated') and its reason: from the trace line the pipeline
    wrote, else computed with stoa.trust.support on the item itself."""
    for t in item.get("trace") or []:
        m = re.match(r"\s*support:\s*(supported|approximate|unlocated)\b\s*(?:[—-]\s*(.*))?", str(t))
        if m:
            return m.group(1), (m.group(2) or "").strip()
    if _num(item.get("value")) is None:
        return None, ""
    try:
        from stoa.contract import Answer, Evidence
        from stoa.trust.support import support_state

        evs = []
        for e in item.get("evidence") or []:
            try:
                evs.append(Evidence(**{k: e.get(k) for k in ("file", "page", "bbox", "bbox_space", "polygon",
                                                              "polyline", "note", "ifc_guid") if e.get(k) is not None}))
            except Exception:  # noqa: BLE001 - one odd box does not sink the item
                continue
        ans = Answer(value=item.get("value"), unit=item.get("unit"),
                     confidence=min(1.0, max(0.0, float(item.get("confidence") or 0))), evidence=evs,
                     trace=[str(t) for t in item.get("trace") or []], solver=str(item.get("method") or ""),
                     assumptions=[str(a) for a in item.get("assumptions") or []],
                     missing=[str(m) for m in item.get("missing") or []])
        s = support_state(question, ans)
        return (s or {}).get("state"), (s or {}).get("why", "")
    except Exception:  # noqa: BLE001
        return None, ""


def signals(item: dict, question: str, tmap: dict | None, page: int = 0) -> dict:
    """The model's inputs for one item (a takeoff row, an answer or an eval row)."""
    kind = kind_of(item.get("unit"), question)
    c = float(item.get("confidence") or 0)
    sup, sup_why = support_of(item, question)
    ag = agree_of(item)
    col = colour_at(tmap, item.get("evidence") or [], page)
    sc = ((tmap or {}).get("scale") or {}).get("status")
    warn = bool(((tmap or {}).get("summary") or {}).get("warning"))
    photo = bool(((tmap or {}).get("summary") or {}).get("photo"))
    negative = bool(((tmap or {}).get("summary") or {}).get("negative"))
    return {"kind": kind, "confidence": round(c, 3), "support": sup, "support_why": sup_why, "agree": ag,
            "disagree_why": disagree_words(item) if ag is False else None,
            "zero": kind == "count" and _num(item.get("value")) == 0,
            "colour": col, "scale": sc, "poor": warn, "photo": photo, "negative": negative,
            "x": {"low": c < 0.5, "mid": 0.5 <= c < 0.7, "unlocated": sup == "unlocated",
                  "approximate": sup == "approximate", "disagree": ag is False, "agree": ag is True,
                  "red": col == "red", "amber": col == "amber",
                  "scale": kind in ("area", "length") and sc is not None and sc != "known",
                  "poor": warn, "area": kind == "area", "length": kind == "length"}}


def p_wrong(x: dict, w: dict | None = None) -> float:
    w = w or WEIGHTS
    z = w.get("bias", 0.0) + sum(w.get(k, 0.0) for k in FEATURES if x.get(k))
    return 1.0 / (1.0 + math.exp(-z))


def reasons(sig: dict, w: dict | None = None, used_scale: float | None = None) -> tuple[list[str], list[str]]:
    """(why to check it, strongest first; what speaks for it). used_scale = the 1:N the takeoff measured with."""
    w = w or WEIGHTS
    x = sig["x"]
    bad = sorted((k for k in REASON if x.get(k) and w.get(k, 0) > 0), key=lambda k: -w.get(k, 0))
    why = []
    for k in bad:
        if k == "disagree" and sig.get("disagree_why"):
            why.append(sig["disagree_why"])
        elif k == "unlocated" and sig.get("zero"):  # a 0 count has nothing to mark (a store with no window is normal)
            why.append("none found: check that none are drawn here")
        elif k == "scale" and used_scale:  # the takeoff measured at 1:N, but the sheet does not confirm it
            why.append(f"measured at 1:{used_scale:g}, a scale not confirmed on the sheet: sizes may be off")
        elif k == "unlocated" and sig.get("support_why"):
            why.append(sig["support_why"][:90])
        elif k == "poor" and sig.get("photo"):  # the trust map's measured phone-photo note, not an ink verdict
            why.append("a phone photo: in our tests about half of such answers were right")
        elif k == "poor" and sig.get("negative"):  # a blueprint: not measured, so no number (trust map t5)
            why.append("a blueprint (white lines on a dark background): few such drawings in our tests")
        elif k in REASON_NO_MAP and not _map_shown():
            why.append(REASON_NO_MAP[k])
        else:
            why.append(REASON[k].format(c=sig["confidence"], scale=_scale_words(sig.get("scale"))))
    good = []
    if x.get("agree"):
        good.append(GOOD["agree"])
    if sig.get("support") == "supported":
        good.append(GOOD["supported"])
    return why, good


def _scale_words(s: str | None) -> str:
    return {"estimated": "estimated, not confirmed", "conflict": "in doubt (two readings differ)",
            "unknown": "not known"}.get(s or "", s or "not known")


def score(item: dict, question: str, tmap: dict | None, page: int = 0, used_scale: float | None = None,
          answer: bool = False) -> dict:
    """{risk, flag, why, good, signals} for one item. F2's review reason / no value / a typical value always flag.
    answer=True (a visitor's own question): flagged by confidence alone (ANSWER_FLAG_CONF), risk = 1 - confidence."""
    sig = signals(item, question, tmap, page)
    v = _num(item.get("value"))
    why, good = reasons(sig, used_scale=used_scale)
    review = item.get("review")
    if v is None or item.get("status") in ("estimate", "timeout", "error", "abstain") or review:
        msg = review or ("no reading: measure it on the drawing" if v is None else
                         "a typical value for this kind of room, not measured")
        msg = re.sub(r"\s*\((?:confidence|conf\.?)\s*[0-9.]+\)", "", str(msg))  # F2's '(confidence 0.35)'
        if "confidence" in str(review or "") or "rough" in msg or v is None:
            # F2's 'rough reading' already says it; a row with no reading has no confidence to speak of
            why = [w for w in why if not w.startswith(("low confidence", "only fairly sure"))]
        if review and sig.get("zero"):  # F2's reason ('no door swing found …') says more than 'none found'
            why = [w for w in why if not w.startswith("none found")]
        if v is None:  # nothing was measured, so no scale could have made it wrong
            why = [w for w in why if not w.startswith(("measured at 1:", "the scale is"))]
        return {"risk": 1.0, "flag": True, "why": [msg, *why][:3], "good": [], "signals": sig, "review": True}
    if answer:
        c = float(sig["confidence"] or 0)
        unsure = c <= ANSWER_UNSURE_CALIBRATED + 1e-9 if _calibrated(item) else c < ANSWER_FLAG_CONF
        return {"risk": round(1.0 - c, 3), "flag": unsure or sig["agree"] is False, "why": why[:3],
                "good": good[:2], "signals": sig, "review": False, "only": None}
    p = p_wrong(sig["x"])
    flag = p >= FLAG_AT or sig["agree"] is False
    return {"risk": round(p, 3), "flag": flag, "why": why[:3], "good": good[:2], "signals": sig, "review": False,
            "only": only_because(sig) if flag else None}


def only_because(sig: dict) -> str | None:
    """'scale' / 'poor' when the item is listed ONLY for that reason (without it the model would not list it): one
    action clears all such rows (set the scale; upload a sharper copy)."""
    x = sig["x"]
    if sig.get("agree") is False:
        return None
    for k in ("scale", "poor"):
        if x.get(k) and p_wrong(dict(x, **{k: False})) < FLAG_AT:
            return k
    return None


# ---- takeoff rows -> check list -----------------------------------------------------------------------------------

def _question(row: dict) -> str:
    if row.get("section") == "equipment":  # F2's piping-plan rows: tagged equipment per family ('Check valves (CV)')
        return f"How many {row.get('item') or 'tagged items'} are there on this sheet?"
    try:
        from stoa.features import takeoff as T

        qs = T.ROOM_QS if row.get("section") == "room" else getattr(T, "SHEET_QS", ())
        for q, text, _u in qs:
            if q == row.get("quantity"):
                return text.format(room=row.get("item") or "room")
    except Exception:  # noqa: BLE001
        pass
    return f"{row.get('quantity') or ''} of {row.get('item') or 'the sheet'}"


def _union(evidence: list[dict], page: int) -> list[float] | None:
    bs = [e.get("bbox") for e in evidence or [] if e.get("bbox") and len(e.get("bbox")) == 4
          and e.get("page") in (None, page)]
    if not bs:
        return None
    return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]


LABEL = {"area": "Floor area", "baseboard": "Skirting", "doors": "Doors", "windows": "Windows",
         "slab_area": "Slab area", "count": "Count"}
UNIT = {"m2": "m²", "ft2": "ft²", "m": "m", "count": ""}


def _item_out(key: str, title: str, what: str, row: dict, sc: dict, page: int, checked: dict,
              section: str = "room", room_ev: dict | None = None) -> dict:
    ev = row.get("evidence") or []
    where = "marks"
    if _union(ev, page) is None and room_ev and room_ev.get("bbox"):
        # nothing marked for this row (a count of 0, no reading): point at its room's outline, so the row still zooms
        ev = [{**room_ev, "note": "the room (nothing is marked for this number)"}]
        where = "room"
    space = next((e.get("bbox_space") for e in ev if e.get("bbox_space")), None)
    ck = checked.get(key)
    return {"id": key, "section": section, "title": title, "what": what, "value": row.get("value"), "where": where,
            "unit": row.get("unit"),
            "unit_label": UNIT.get(row.get("unit") or "", row.get("unit") or ""),
            "quantity": row.get("quantity"), "risk": sc["risk"], "flag": sc["flag"], "review": sc["review"],
            "why": sc["why"], "good": sc["good"], "colour": sc["signals"]["colour"],
            "support": sc["signals"]["support"], "agree": sc["signals"]["agree"],
            "confidence": sc["signals"]["confidence"], "checked": ck, "zoom": _union(ev, page), "space": space,
            "only": sc.get("only"),
            "evidence": [{k: e.get(k) for k in ("bbox", "bbox_space", "polygon", "page", "note") if e.get(k) is not None}
                         for e in ev][:60]}


# ---- rooms: where to look for a row with no mark, and whether a window is expected there ----------------------------
# A count of 0 has nothing to mark, so T1's 'unlocated' does not apply to it. In the eval every 0 answer was wrong
# (19 / 19 on d28 + d29: questions ask about things that exist), but a takeoff lists EVERY room, and most rooms with no
# window are inside the building or are stores. So a takeoff row 'Windows: 0' is a rule, not the fitted model: it is
# listed when the room is a habitable one (F2's room_type) on an outside wall (>= FACADE_MIN_M of its outline along
# the building's outer edge), and more so when rooms of the same name have a window. Checked on DfE Level 01: 20 of the
# 22 rooms where windows were found lie on the facade; of its nine 0-window rows only Group Room (1) and (5) qualify,
# the twins of Group Room (3) and (4), which have one window each. Not fitted: there is no per-room window gold.
OCR_TAG_NOTE = "tags read by OCR from the picture: a misread or missed tag changes the count"
# F2's piping-plan rows (section 'equipment') are counted two ways (its assumptions[0] says which): DISTINCT IDs
# ('counted distinct IDs: CV-1, CV-2, …': a tag printed twice counts once, so a missed duplicate does not matter, but the
# numbering shows a gap when one is missed) or EVERY printed label ('counted every printed symbol label: 21 x PG': no
# redundancy, one missed or misread label changes the count). Rules, not fitted (one sheet: Stoa's M-101A, PDF vs PNG):
# a gap of <= GAP_MAX numbers in a distinct-ID family lists the row; an every-label count read by OCR from a picture lists
# it. M-101A PDF: 1 of 9 listed (PS-n: no PS-2; the gold 23 says it is not used); PNG: 4 of 9, incl. both rows that differ
# from the PDF (PG 20 vs 21, PS 4 vs 3).
GAP_MAX = 3
EVERY_LABEL_OCR = ("every printed label counted, read by OCR from the picture: one missed or misread label changes "
                   "the count")


def score_equipment(r: dict, sc: dict) -> dict:
    a0 = str((r.get("assumptions") or [""])[0])
    picture = bool(r.get("_px")) or "OCR" in str(r.get("method") or "")
    if a0.startswith("counted distinct IDs") and "…" not in a0:
        ids = re.findall(r"\b([A-Z]+)-(\d+)\b", a0)
        fam = collections.Counter(f for f, _ in ids).most_common(1)
        if fam:
            f = fam[0][0]
            nums = {int(n) for g, n in ids if g == f}
            mx = max(nums)
            gaps = sorted(set(range(1, mx + 1)) - nums)
            if gaps and len(gaps) <= GAP_MAX:
                g = ", ".join(f"{f}-{n}" for n in gaps)
                why = f"no {g} on the sheet between {f}-1 and {f}-{mx}: a tag missed, or not used here?"
                return {**sc, "flag": True, "risk": max(sc["risk"], 0.5), "only": None, "why": [why, *sc["why"]][:3]}
    if a0.startswith("counted every printed") and picture:
        return {**sc, "flag": True, "risk": max(sc["risk"], 0.45), "only": None,
                "why": [EVERY_LABEL_OCR, *sc["why"]][:3]}
    return sc
ONE_ACTION_MIN = 5  # the summary names one action (set the scale) when it would clear at least this many rows
WINDOW_EXPECTED = {"teaching", "living", "kitchen", "hall", "staff", "sleep"}
NO_WINDOW_WORDS = {"wet": "a toilet or wet room", "circulation": "a stair, lift or corridor", "storage": "a store",
                   "plant": "a plant room"}
FACADE_MIN_M = 1.5
CLOSE_M = 3.0


def rooms_geometry(rows: list[dict]) -> dict:
    """(item, copy) -> {'ev': the room's outline evidence, 'facade_m': metres of its outline along the building's
    outer edge, or None}. The building = every traced room outline grown by 0.5 m (closes the walls between rooms),
    notches up to 2 x CLOSE_M wide closed; the facade band = its outer edge ± 0.9 m. Drawing units per metre come from the outlines' own areas vs their m²
    (median), so pdf_pt and image_px work alike."""
    out: dict = {}
    polys: dict = {}
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:  # noqa: BLE001
        Polygon = None  # noqa: N806
    for r in rows or []:
        if r.get("section") != "room" or r.get("quantity") != "area":
            continue
        k = (r.get("item"), r.get("copy", 1))
        evs = [e for e in r.get("evidence") or [] if e.get("bbox") and len(e.get("bbox")) == 4]
        ev = next((e for e in evs if e.get("polygon") and len(e["polygon"]) >= 3), evs[0] if evs else None)
        if not ev:
            continue
        out[k] = {"ev": {kk: ev.get(kk) for kk in ("bbox", "bbox_space", "polygon", "page") if ev.get(kk) is not None},
                  "facade_m": None}
        v = _num(r.get("value"))
        if Polygon is None or not ev.get("polygon") or not v or v <= 0:
            continue
        try:
            pl = Polygon(ev["polygon"])
            pl = pl if pl.is_valid else pl.buffer(0)
            if pl.area > 0:
                polys[k] = (pl, v)
        except Exception:  # noqa: BLE001
            continue
    if len(polys) < 4:  # too few rooms to tell the building's edge
        return out
    try:
        ups = sorted(math.sqrt(pl.area / v) for pl, v in polys.values())
        upm = ups[len(ups) // 2]
        fp = unary_union([pl.buffer(0.5 * upm) for pl, _ in polys.values()])
        # close notches up to ~6 m wide: a space with no traced outline (toilet cubicles, a stair drawn without a
        # label) otherwise opens a false 'outside wall' into the building (DfE L01: Group Room (1) under the toilets)
        fp = fp.buffer(CLOSE_M * upm).buffer(-CLOSE_M * upm)
        edge = unary_union([g.exterior for g in getattr(fp, "geoms", [fp])])
        band = edge.buffer(0.9 * upm)
        for k, (pl, _) in polys.items():
            out[k]["facade_m"] = round(pl.exterior.intersection(band).length / upm, 1)
    except Exception:  # noqa: BLE001 - no facade test: the rows keep the model's score
        pass
    return out


def score_zero_windows(r: dict, q: str, tmap: dict | None, page: int, geo: dict | None,
                       siblings: list) -> dict:
    """'Windows: 0' on a takeoff room (see WINDOW_EXPECTED above)."""
    sig = signals(r, q, tmap, page)
    sig["x"] = dict(sig["x"], unlocated=False)
    p = p_wrong(sig["x"])
    why, good = reasons(sig)
    fm = (geo or {}).get("facade_m")
    typ = r.get("room_type")
    if fm is not None and fm >= FACADE_MIN_M and typ in WINDOW_EXPECTED:
        lead = [f"on an outside wall ({fm:g} m of facade), but no window found"]
        if siblings:
            names = ", ".join(f"({c})" for c in siblings[:3]) + (" …" if len(siblings) > 3 else "")
            lead.append(f"{r.get('item')} {names} {'has' if len(siblings) == 1 else 'have'} one")
        why = [*lead, *why]
        p = max(p, 0.6 if siblings else 0.45)
        return {"risk": round(p, 3), "flag": True, "why": why[:3], "good": [], "signals": sig, "review": False}
    if fm is not None and fm < FACADE_MIN_M:
        good = ["no outside wall: no window expected", *good]
    elif typ in NO_WINDOW_WORDS:
        good = [f"{NO_WINDOW_WORDS[typ]}: often has no window", *good]
    return {"risk": round(p, 3), "flag": p >= FLAG_AT, "why": why[:3], "good": good[:2], "signals": sig,
            "review": False}


def user_ratio(tmap: dict | None, label: str | None) -> float | None:
    """The 1:N of a scale the visitor set (the label '1:200.02' the web sends, else the trust map's remembered one)."""
    m = re.search(r"1\s*:\s*([0-9]+(?:\.[0-9]+)?)", label or "")
    if m:
        return float(m.group(1))
    sc = (tmap or {}).get("scale") or {}
    return _num(sc.get("ratio")) if sc.get("source") == "user" else None


SIZE_Q = {"area", "baseboard", "length", "slab_area", "perimeter"}


def takeoff_upm(rows: list[dict]) -> tuple[float, str] | None:
    """(drawing units per metre, their space) the takeoff measured with: the median of sqrt(outline area / m²) over
    its room rows (pdf_pt or image_px, whatever the evidence is in). None without >= 3 outlines."""
    vals, space = [], None
    for r in rows or []:
        if r.get("section") != "room" or r.get("quantity") != "area" or not (_num(r.get("value")) or 0) > 0:
            continue
        e = next((e for e in r.get("evidence") or [] if e.get("polygon") and len(e["polygon"]) >= 3), None)
        if not e:
            continue
        pts = e["polygon"]
        a = abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2
        if a > 0:
            vals.append(math.sqrt(a / float(r["value"])))
            space = space or e.get("bbox_space")
    if len(vals) < 3:
        return None
    vals.sort()
    return vals[len(vals) // 2], space or "pdf_pt"


def score_inner_windows(r: dict, q: str, tmap: dict | None, page: int, geo: dict | None) -> dict | None:
    """'Windows: N > 0' on a room with NO outside wall: the reader likely counted a window of a neighbour's wall (F2: 'a
    window between two rooms counts for both'). Esplanades floors 1-2 vs the IFC model (experiments/f6/
    checklist_windows_ifc.py): 31 of 33 such rows were wrong (toilets with 4 windows, halls, the lift), facade rooms
    13 of 36 right. None = not this case (the model scores the row)."""
    fm = (geo or {}).get("facade_m")
    v = _num(r.get("value"))
    if fm is None or fm >= FACADE_MIN_M or not v or v <= 0:
        return None
    sc = score(r, q, tmap, page)
    n = f"{v:g} window{'s' if v != 1 else ''}"
    return {**sc, "risk": round(max(sc["risk"], 0.6), 3), "flag": True, "only": None, "good": [],
            "why": [f"{n} found, but the room has no outside wall: a window of a neighbouring room may have been "
                    "counted", *sc["why"]][:3]}


def build(st: dict, tmap: dict | None, checked: dict | None = None, page: int = 0,
          user_n: float | None = None, user_upm: tuple[float, str] | None = None,
          drawing_upm: tuple[float, str] | None = None) -> dict:
    """The check list of a finished takeoff (takeoff.status() with status 'done'). The scale the visitor set: user_upm
    = (units per metre, space) from the measure tool, else user_n = its 1:N. When it is not the scale the takeoff
    measured with (> 3 % apart), every size is listed with the factor (the takeoff used the other scale).
    drawing_upm = the scale the web read off a PICTURE itself (its scale bar / printed text: in image space the measure
    tool never guesses); when it agrees with the takeoff's within 3 %, the scale counts as confirmed (two readings)."""
    checked = checked or {}
    items = []
    rows = st.get("rows") or []
    took = _num(st.get("scale_n"))
    off, mine = None, None
    t_upm = takeoff_upm(rows) if (user_upm or drawing_upm) else None
    sc0 = (tmap or {}).get("scale") or {}
    if (drawing_upm and not user_upm and t_upm and drawing_upm[1] == "image_px" == t_upm[1] and drawing_upm[0] > 0
            and sc0.get("status") != "known" and abs(t_upm[0] / drawing_upm[0] - 1) <= 0.03):
        tmap = {**(tmap or {}), "scale": {**sc0, "status": "known", "source": "drawing+takeoff",
                                          "why": "the scale read off the drawing and the takeoff's agree (within "
                                                 f"{max(1, round(100 * abs(t_upm[0] / drawing_upm[0] - 1)))} %)"}}
    if user_upm and t_upm and user_upm[0] > 0 and user_upm[1] == t_upm[1]:
        off = t_upm[0] / user_upm[0]  # a length the takeoff measured, in the visitor's scale
        mine = f"1 m = {user_upm[0]:.0f} px" if user_upm[1] == "image_px" else f"1:{2834.6457 / user_upm[0]:.0f}"
    elif user_n and took:
        off, mine = user_n / took, f"1:{user_n:g}"
    off = off if off and abs(off - 1) > 0.03 else None
    geo = rooms_geometry(rows)
    with_window: dict = {}
    for r in rows:
        if r.get("section") == "room" and r.get("quantity") == "windows" and (_num(r.get("value")) or 0) > 0:
            with_window.setdefault(r.get("item"), []).append(r.get("copy", 1))
    for r in rows:
        if r.get("section") not in ("room", "slab", "sheet", "printed", "equipment"):
            continue
        if r.get("section") == "printed" and _num(r.get("value")) is None:
            continue
        q = _question(r)
        k = (r.get("item"), r.get("copy", 1))
        if (r.get("section") == "room" and r.get("quantity") == "windows" and _num(r.get("value")) == 0
                and not r.get("review") and r.get("status") not in ("estimate", "timeout", "error", "abstain")):
            sc = score_zero_windows(r, q, tmap, page, geo.get(k),
                                    [c for c in with_window.get(r.get("item"), []) if c != r.get("copy", 1)])
        else:
            sc = None
            if r.get("section") == "room" and r.get("quantity") == "windows" and not r.get("review"):
                sc = score_inner_windows(r, q, tmap, page, geo.get(k))
            sc = sc or score(r, q, tmap, page, _num(st.get("scale_n")))
        if off and r.get("quantity") in SIZE_Q and _num(r.get("value")) is not None:
            k2 = off ** 2 if r.get("quantity") in ("area", "slab_area") else off
            was = f"1:{took:g}" if took else "its own scale"
            sc = {**sc, "risk": 1.0, "flag": True, "only": None, "good": [],
                  "why": [f"measured at {was}, but you set {mine}: this value would be ×{k2:.2f} "
                          "(run the takeoff again with your scale)", *sc["why"]][:3]}
        if r.get("section") == "equipment" and not r.get("fixed"):
            sc = score_equipment(r, sc)
        if r.get("fixed"):  # a person measured it (F2's fix): counts as checked
            sc = {**sc, "flag": False, "why": [], "good": [f"measured by a person at {r.get('fixed')}"]}
        lab = LABEL.get(r.get("quantity"), str(r.get("quantity") or ""))
        name = str(r.get("item") or "")
        if r.get("copies", 1) and int(r.get("copies") or 1) > 1:
            name += f" ({r.get('copy', 1)})"
        items.append(_item_out(str(r.get("id") or f"r{len(items)}"), name, lab, r, sc, page, checked,
                               str(r.get("section") or "room"),
                               (geo.get(k) or {}).get("ev") if r.get("section") == "room" else None))
        if r.get("fixed"):  # measured with the Measure tool: shown under 'Checked by you'
            items[-1]["checked"] = f"measured {r.get('fixed')}"
        if (r.get("section") == "equipment" and (r.get("_px") or "OCR" in str(r.get("method") or ""))
                and EVERY_LABEL_OCR not in items[-1]["why"]):
            # F2's piping rows on a PICTURE count tags its OCR read (Stoa's M-101A PNG: 2 of 9 families off by one vs
            # the PDF's text); not a fitted flag (one sheet), a caveat shown on the row even when it is low-risk
            items[-1]["note"] = OCR_TAG_NOTE
    return finish(items, st, tmap)


def finish(items: list[dict], st: dict | None, tmap: dict | None) -> dict:
    """Order (flagged first, riskiest first), the summary line and the totals without the unresolved rows."""
    for it in items:
        it["state"] = "checked" if it.get("checked") else ("check" if it["flag"] else "ok")
    order = {"check": 0, "checked": 1, "ok": 2}
    items.sort(key=lambda it: (order[it["state"]], -it["risk"]))
    todo = [it for it in items if it["state"] == "check"]
    ok = [it for it in items if it["state"] == "ok"]
    done = [it for it in items if it["state"] == "checked"]
    n = len(items)
    agree = sum(1 for it in ok if it["agree"])
    sup = sum(1 for it in ok if it["support"] == "supported")
    if not n:
        line = "Nothing to check yet: the takeoff has no readings."
    elif not todo:
        line = f"Nothing left to check: all {n} readings look low-risk" + (f" ({len(done)} checked by you)." if done else ".")
    else:
        why_ok = []
        if agree:
            why_ok.append(f"{agree} agree by two methods")
        if sup:
            why_ok.append(f"{sup} have a printed value or one mark per item")
        rest = f"the other {len(ok)} look low-risk" + (f" ({'; '.join(why_ok)})" if why_ok else "") if ok else ""
        line = f"Check these {len(todo)} of {n} first" + (f" — {rest}" if rest else "") + "."
    only_scale = sum(1 for it in todo if it.get("only") == "scale")
    only_poor = sum(1 for it in todo if it.get("only") == "poor")
    hint = None
    if only_scale >= ONE_ACTION_MIN:
        hint = (f"{only_scale} of them are listed only because the scale is not confirmed on the sheet: set it once "
                "(Measure → Calibrate, two clicks on a length you know) and they leave the list.")
    elif only_poor >= ONE_ACTION_MIN and ((tmap or {}).get("summary") or {}).get("photo"):
        hint = (f"{only_poor} of them are listed only because this is a phone photo: the PDF or a flat scan would be "
                "read more reliably.")
    elif only_poor >= ONE_ACTION_MIN and ((tmap or {}).get("summary") or {}).get("negative"):
        hint = (f"{only_poor} of them are listed only because this is a blueprint: our readers were tested on few "
                "drawings like it.")
    elif only_poor >= ONE_ACTION_MIN:
        hint = (f"{only_poor} of them are listed only because this sheet reads poorly: the original PDF or a sharper "
                "scan would be read more reliably.")
    return {"ok": True, "version": VERSION, "status": "done", "items": items,
            "summary": {"n": n, "check": len(todo), "checked": len(done), "ok": len(ok), "line": line,
                        "only_scale": only_scale, "only_poor": only_poor, "hint": hint,
                        "flag_at": FLAG_AT, "calibration": calib_line()},
            "totals": totals(items), "scale": ((tmap or {}).get("scale") or {}).get("status"),
            "scale_why": ((tmap or {}).get("scale") or {}).get("why"),
            "sheet_warning": ((tmap or {}).get("summary") or {}).get("warning"),
            "sheet_photo": bool(((tmap or {}).get("summary") or {}).get("photo")),
            "sheet_negative": bool(((tmap or {}).get("summary") or {}).get("negative")),
            "job": (st or {}).get("job"), "file": (st or {}).get("file"), "page": (st or {}).get("page")}


def totals(items: list[dict]) -> list[dict]:
    """Per quantity: the sum of rows that look sound or were checked; the unresolved rows are summed apart."""
    out = []
    for q, lab in (("area", "Net floor area"), ("baseboard", "Skirting"), ("doors", "Doors"), ("windows", "Windows")):
        rooms = [it for it in items if it["quantity"] == q and it.get("section") == "room"
                 and _num(it.get("value")) is not None]
        if not rooms:
            continue
        units = {it["unit"] for it in rooms}
        for u in sorted(units, key=str):
            rs = [it for it in rooms if it["unit"] == u]
            inn = [it for it in rs if it["state"] != "check"]
            outr = [it for it in rs if it["state"] == "check"]
            out.append({"quantity": q, "label": lab, "unit": u, "unit_label": UNIT.get(u or "", u or ""),
                        "value": round(sum(float(it["value"]) for it in inn), 2), "rows": len(inn),
                        "waiting": round(sum(float(it["value"]) for it in outr), 2), "waiting_rows": len(outr)})
    return out


# the same rule measured on the build with T1's calibrated confidence (d32: answers listed at calibrated conf <= 0.6),
# shown when this build calibrates (stoa/trust/calibrate.py present, STOA_T1_CALIBRATE not 0): answers WITH a number only
# (score_answers skips a no-value answer); experiments/f6/answer_sweep.py VALUE=1 on S0's d32 gate runs
CALIBRATION_T1 = {"split": "d32 gate (S0 45cbee21: stoa-like dev, upload path, Gemini off), calibrated conf <= 0.6",
                  "n": 312, "sheets": 32, "flagged": 81, "flagged_wrong": 39, "rest": 231, "rest_wrong": 17, "wrong": 56,
                  "wrong_in_flagged": 39}


def _t1_build() -> bool:
    """True when this build gives answers T1's calibrated confidence (d32+), i.e. the list uses the <= 0.6 rule."""
    if os.environ.get("STOA_T1_CALIBRATE", "1") == "0":
        return False
    try:
        import importlib.util

        return importlib.util.find_spec("stoa.trust.calibrate") is not None
    except Exception:  # noqa: BLE001
        return False


def calib_line() -> str | None:
    c = CALIBRATION_T1 if _t1_build() else CALIBRATION
    if not c.get("n") or not c.get("flagged") or not c.get("rest"):
        return None
    fw = round(100 * c["flagged_wrong"] / c["flagged"])
    rw = round(100 * c["rest_wrong"] / c["rest"])
    cov = round(100 * c["wrong_in_flagged"] / max(1, c["wrong"]))
    return (f"In our tests ({c['n']} answers on {c.get('sheets', 'many')} sheets), {fw} % of the flagged answers were "
            f"wrong, {rw} % of the others; the list caught {cov} % of the wrong ones.")


# ---- the visitor's answers -----------------------------------------------------------------------------------------

def score_answers(answers: list[dict], tmap: dict | None, page: int, checked: dict | None = None) -> list[dict]:
    checked = checked or {}
    out = []
    for i, a in enumerate(answers[:60]):
        q = str(a.get("question") or "")[:300]
        if _num(a.get("value")) is None:
            continue
        sc = score(a, q, tmap, page, answer=True)
        key = "a" + hashlib.sha1(q.encode()).hexdigest()[:10]
        it = _item_out(key, q, "Your question", a, sc, page, checked, "visit")
        out.append(it)
    return out


# ---- a person's ticks (per visitor + sheet) -------------------------------------------------------------------------

def checks_dir() -> Path:
    d = Path(os.environ.get("STOA_CHECKLIST_DIR") or Path(tempfile.gettempdir()) / "stoa-checklist")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ck_path(path: str, page: int, visitor: str | None) -> Path:
    v = visitor if visitor and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", visitor) else "anon"
    h = hashlib.sha1(f"{path}#{int(page)}#{v}".encode()).hexdigest()[:20]
    return checks_dir() / f"{h}.json"


def load_checks(path: str, page: int, visitor: str | None) -> dict:
    try:
        return json.loads(_ck_path(path, page, visitor).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def forget_checks(path: str, visitor: str | None, pages: int = 200) -> int:
    """'Start over' (captain 01:18, W5's 'stoa:reset' with forgetCorrections): drop this visitor's ticks on every page of
    the sheet. Only for a real visitor id: the shared 'anon' file (scripts, tests) is never wiped from a browser."""
    if not (visitor and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", visitor)):
        return 0
    n = 0
    for pg in range(max(1, min(int(pages), 500))):
        f = _ck_path(path, pg, visitor)
        if f.exists():
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    return n


def set_check(path: str, page: int, visitor: str | None, item_id: str, checked: bool) -> dict:
    if not re.fullmatch(r"[ra][0-9a-f]{1,12}|r\d{1,5}", item_id or ""):
        raise ValueError("bad item id")
    ck = load_checks(path, page, visitor)
    if checked:
        ck[item_id] = time.strftime("%H:%M")
    else:
        ck.pop(item_id, None)
    p = _ck_path(path, page, visitor)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ck))
    tmp.replace(p)
    return ck


# ---- glue ---------------------------------------------------------------------------------------------------------

def _tmap(abs_path: str, page: int, user_scale: str | None = None, rel: str | None = None,
          visitor: str | None = None) -> dict | None:
    """The trust map (colours, scale status, sheet warning) incl. a scale the visitor set: the label the web sends
    (Measure → Calibrate), else one F7 remembered for this visitor (trust_map._user_scale)."""
    try:
        from stoa.features.trust_map import trust_map

        return trust_map(abs_path, page, "auto", user_scale=user_scale, rel=rel, visitor=visitor)
    except Exception:  # noqa: BLE001 - the check list works without colours
        return None


_VISITOR_RX = __import__("re").compile(r"[A-Za-z0-9_-]{8,64}")


def _vkw(fn, visitor: str | None) -> dict:
    """{'visitor': id} for F2's per-visitor takeoff calls (night-F2 1ef28b65: status / start / fix keep each person's
    Measure fixes apart) when this takeoff accepts it and the id is valid; {} for an older takeoff or no / bad id."""
    if not visitor or not _VISITOR_RX.fullmatch(str(visitor)):
        return {}
    try:
        import inspect

        return {"visitor": visitor} if "visitor" in inspect.signature(fn).parameters else {}
    except (TypeError, ValueError):
        return {}


def for_sheet(path: str, page: int = 0, visitor: str | None = None, user_scale: str | None = None,
              user_upm: tuple[float, str] | None = None, drawing_upm: tuple[float, str] | None = None) -> dict:
    """The check list of the sheet's takeoff (finished), or the takeoff's progress."""
    from stoa.features import takeoff as T

    abs_path, virtual = T.resolve(path)
    key = T.job_key(abs_path, page)
    st = T.status(key, virtual, **_vkw(T.status, visitor)) if (T.cache_dir() / key).exists() else \
        {"status": "none", "job": key}
    if st.get("status") != "done":
        return {"ok": True, "status": "none" if st.get("status") == "unknown" else st.get("status"), "job": key,
                "file": virtual, "page": page, "done": st.get("done", 0), "total": st.get("total", 0),
                "current": st.get("current", ""), "error": st.get("error")}
    tm = _tmap(abs_path, page, user_scale, virtual, visitor)
    return build(st, tm, load_checks(virtual, page, visitor), page, user_ratio(tm, user_scale), user_upm, drawing_upm)


def enabled() -> bool:
    from stoa.features import enabled as on

    return "checklist" in on()


try:
    from fastapi import APIRouter, HTTPException, Query
    from pydantic import BaseModel

    router = APIRouter(prefix="/feat/checklist", tags=["checklist"])

    class StartReq(BaseModel):
        path: str
        page: int = 0
        visitor: str | None = None

    class AnswersReq(BaseModel):
        path: str
        page: int = 0
        answers: list[dict] = []
        visitor: str | None = None
        user_scale: str | None = None

    class MeasureReq(BaseModel):
        path: str
        page: int = 0
        id: str
        value: float | None = None  # None = undo (back to our reading)
        unit: str | None = "m2"
        polygon: list[list[float]] | None = None
        space: str | None = None
        visitor: str | None = None

    class CheckReq(BaseModel):
        path: str
        page: int = 0
        id: str
        checked: bool = True
        visitor: str | None = None

    class ForgetReq(BaseModel):
        path: str
        visitor: str | None = None

    def _guard() -> None:
        if not enabled():
            raise HTTPException(404, "checklist feature is off (STOA_FEATURES)")

    @router.get("")
    def get_ep(path: str = Query(...), page: int = 0, visitor: str | None = None,
               user_scale: str | None = None, user_upm: float | None = None, user_space: str | None = None,
               drawing_upm: float | None = None, drawing_space: str | None = None) -> dict:
        _guard()
        try:
            def pair(u, sp):
                return (float(u), sp) if u and u > 0 and sp in ("pdf_pt", "image_px") else None

            return for_sheet(path, page, visitor, (user_scale or "")[:40] or None, pair(user_upm, user_space),
                             pair(drawing_upm, drawing_space))
        except Exception as e:  # noqa: BLE001 - fail closed
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    @router.post("/start")
    def start_ep(req: StartReq) -> dict:
        _guard()
        try:
            from stoa.features import takeoff as T

            st = T.start(req.path, req.page, **_vkw(T.start, req.visitor))
            return {"ok": True, **{k: st.get(k) for k in ("job", "status", "done", "total", "current", "error")}}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    @router.post("/measure")
    def measure_ep(req: MeasureReq) -> dict:
        """A person measured a listed AREA row with the Measure tool: saved as F2's fix (takeoff.fix: the row becomes
        'measured by a person' and goes back into the totals for everyone looking at this takeoff); then the list."""
        _guard()
        try:
            from stoa.features import takeoff as T

            abs_path, virtual = T.resolve(req.path)
            key = T.job_key(abs_path, req.page)
            st = T.status(key, virtual, **_vkw(T.status, req.visitor))
            row = next((r for r in st.get("rows") or [] if r.get("id") == req.id), None)
            if not row or row.get("quantity") != "area" or row.get("section") != "room":
                return {"ok": False, "error": "only a room's floor area can be measured here"}
            # the fix is this visitor's own (F2 1ef28b65): the next judge does not see it as 'measured by a person'
            T.fix(key, req.id, req.value, req.unit or "m2", req.polygon, req.space, row.get("item"),
                  **_vkw(T.fix, req.visitor))
            return for_sheet(req.path, req.page, req.visitor)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    @router.post("/answers")
    def answers_ep(req: AnswersReq) -> dict:
        _guard()
        try:
            from stoa.features import takeoff as T

            abs_path, virtual = T.resolve(req.path)
            items = score_answers(req.answers, _tmap(abs_path, req.page, (req.user_scale or "")[:40] or None, virtual,
                                                     req.visitor), req.page,
                                  load_checks(virtual, req.page, req.visitor))
            for it in items:
                it["state"] = "checked" if it.get("checked") else ("check" if it["flag"] else "ok")
            return {"ok": True, "items": items}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    @router.post("/check")
    def check_ep(req: CheckReq) -> dict:
        _guard()
        try:
            from stoa.features import takeoff as T

            _abs, virtual = T.resolve(req.path)
            ck = set_check(virtual, req.page, req.visitor, req.id, req.checked)
            return {"ok": True, "checked": ck}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    @router.post("/forget")
    def forget_ep(req: ForgetReq) -> dict:
        _guard()
        try:
            from stoa.features import takeoff as T

            _abs, virtual = T.resolve(req.path)
            return {"ok": True, "forgot": forget_checks(virtual, req.visitor)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
except ImportError:  # the library works without FastAPI
    router = None
