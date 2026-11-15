"""T1 c7 (captain 00:31, AUDIT RECHECK-0026 item 6): the shown confidence = P(right), refitted on what the answer rests on.

The readers' own confidences rank answers well inside one reader, but not across them: a vector room area measured at
the sheet's printed 1:N sat at 0.5 ('Best estimate') and was right 83 % of the time, while a count whose only basis is
the vision model's estimate sat at 0.35 and was right 21 % of the time. The support line (stoa.trust.support: supported
/ approximate / unlocated + why) says what the number rests on, so the final confidence is a small logistic map:

    p = sigmoid(w · x),  x = logit(reader confidence), support state, basis (printed value / one mark per item /
                             traced outline / not located / …), kind (count / area / length), medium (vector PDF /
                             picture / scan / IFC-DXF), 'two methods agree'

fitted by experiments/t1/calib_fit.py on d30's upload runs (stoa-like + wild + scan, dev and sealed; cross-fitted
numbers in that script's output). Applied ONCE, as the last step of the upload solver (stoa/solvers/upload.py), after
every decision that reads a confidence (vector trace <= 0.3, image pass, Gemini escalation), so no value changes.

Rules on top of the fit (the card's bands: >= 0.9 'Very sure', >= 0.7 'Fairly sure', < 0.4 'Our guess', < 0.25 'Rough
guess'):
- unlocated answers stay < 0.4 (shown as a guess: nothing on the drawing backs them), and so do answers a later
  support check contradicts ('contra' basis: right <= 25 % on replays); marks that may not be distinct objects <= 0.6,
- only a supported, LABELLED printed value may reach 0.9 ('Very sure'); a bare printed number <= 0.75; a guard's cap that withheld a number (value None) is left alone,
- text answers, abstentions and answers without a support line are not touched,
- the dataset's own drawing families read by the reader built for them (FAMILY_READERS): never below that reader's own
  confidence (the map may raise it). Kill switch STOA_T1_FAMILY=0.
Kill switch: STOA_T1_CALIBRATE=0.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

# basis of the answer, from the support line's why (stoa.trust.support.mark_support); first match wins
_BASIS = [
    ("doubt", re.compile(r"may be counted twice|may be a misread|tag found\): check the drawing|may be unread|is a drawing's title", re.I)),  # support.distinct_marks (sl-041 'PS-0'), series_gap (sl-340 'English 8')
    # support checks of T1 c5 / c6 that are NOT in d30's runs (so not in the fit), each measured mostly WRONG on replays
    # of ~800 prediction files: an unlabelled mm dimension as a length (6 right / 24 wrong, audit_baredim.py), the sheet
    # prints a different count (0 / 4, audit_printedcount.py), all marks are one tag family the question does not name
    # (0 / 5, xr-43). NOT the scale-bar re-measure (support.scale_bar_check, 0 / 12 on my replay): F8 00:27 measured
    # its at-scale verdict on 764 runs as a 2x signal (72 % wrong), not a veto -> it stays a support line only
    # T1 c8: a count whose one mark is a heading / note fragment / plural label ('RESTROOMS,', 'SLOW SAND FILTERS'):
    # replay of 840 upload files 0 right / 10 wrong (experiments/t1/audit_plural.py)
    ("contra", re.compile(r"has no label: check|the sheet prints a different count|-tagged: .* are not counted|"
                          r"not the name of one item|in the plural: it may stand|not checked as objects one by one", re.I)),
    ("partial", re.compile(r"showing \d+ of \d+|at least \d+ located", re.I)),
    ("uncounted", re.compile(r"not counted", re.I)),
    ("prior", re.compile(r"typical value", re.I)),
    ("nofound", re.compile(r"none found", re.I)),
    ("notprinted", re.compile(r"does not print this number|not printed", re.I)),
    ("nothing", re.compile(r"nothing on the drawing is marked", re.I)),
    # before 'printed' / 'geometry': these whys say what the answer is NOT ('…, not read from the drawing's own data')
    ("traced", re.compile(r"outline is traced|traced or estimated", re.I)),
    ("readat", re.compile(r"where it was read", re.I)),
    ("printed", re.compile(r"printed (value|total)|marked printed values|checked printed|printed area table|"
                           r"number is printed", re.I)),
    ("marks", re.compile(r"marks?, one per counted item", re.I)),
    ("geometry", re.compile(r"drawing's own (geometry|data)|measured from", re.I)),
]
BASES = [b for b, _ in _BASIS] + ["bare"]
# 'the printed value is marked: '1448'': a bare number (no label, no word) is the weakest printed basis (d30 runs:
# right 37 / 50 vs 170 / 180 for a labelled line: '1448' was 14.48 m read as a room length, '2388m2' the site area)
_QUOTE = re.compile(r"marked: '(.*)'", re.S)
_BARE = re.compile(r"\s*[-\d.,'\" x×/]+\s*(m|mm|cm|m2|m²|ft|sf)?\s*", re.I)
STATES = ("supported", "approximate", "unlocated")
KINDS = ("count", "area", "length")
MEDIA = ("vector", "picture", "scan", "structured")

_AREA_U = re.compile(r"^(m2|m²|ft2|ft²|sf|sqft|sq ?ft|sq ?m|gsf|nsf|ha|mm2|cm2)$", re.I)
_LEN_U = re.compile(r"^(m|mm|cm|km|ft|feet|foot|in|inch|inches|yd|lf)$", re.I)
_AGREE = re.compile(r"independent methods agree|methods agree|cross-check[^\n]*✓|= this count ✓|agrees? with", re.I)

_CAP = re.compile(r"capped at|confidence ≤|confidence <=|confidence -> \d|-> confidence", re.I)

COEF_PATH = Path(__file__).with_name("calibrate_coef.json")
_COEF: dict | None = None

TRACE_PREFIX = "calibration (T1):"
_KEPT_OWN = re.compile(r"its own calibrated confidence [0-9.]+ is kept: >=")  # sanity.uncounted_guard (T1 c7, V1 01:30)
UNLOCATED_MAX = 0.39  # < web LOW_CONF 0.4: an answer nothing on the drawing backs is shown as 'Our guess'
NOT_SUPPORTED_MAX = 0.89  # 'Very sure' (>= 0.9) only for a supported, labelled printed value
DOUBT_MAX = 0.6  # = support.DOUBT_CONF: marks that may hold a misread / a double never read 'Fairly sure'
BARE_MAX = 0.75  # a bare printed number ('1448', '9'-0"'): right 37 / 50 on d30's runs
CONTRA_MAX = 0.39  # 'contra' basis (right <= 25 % on replays): shown as 'Our guess'
# T1 c9 (V1 02:16: 11,891 dataset files uploaded alone, d32): the dataset's own drawing families, read by the reader that
# was built and tested on them, are right 97-99.5 % at that reader's own 0.93-0.95 (swiss_plan / pdf_vector counts 2,478
# rows 99.5 %, areas 1,228 rows 96.9 %; bim_plan / plan_rooms areas with an agreeing cross-check 1,206 rows 99.3 %; zero
# counts 'nothing to mark' 32 / 32); the map (fitted on Stoa-like sheets) showed them at 0.87 and zero counts at 0.39.
# These kinds were recognised on 0 of 35,488 Stoa-like / wild / scan / sealed upload rows. Kill switch STOA_T1_FAMILY=0.
FAMILY_READERS = {"swiss_plan": ("pdf_vector", "dxf"), "bim_plan": ("plan_rooms", "ifcplan_pdf")}
# keep-own's evidence (V1's 2,262 dataset rows, 98.5 % right) is CubiCasa-style pictures only: cubicasa_plan 2,123,
# plan_render 46, 'scan' 93. Elsewhere (a real-world sheet the reader is not trained on) the unlocated cap stays.
OWN_KINDS = ("cubicasa_plan", "plan_render")


def _support_line(ans) -> tuple[str | None, str]:
    s = getattr(ans, "support", None)
    if isinstance(s, dict) and s.get("state"):
        return str(s["state"]), str(s.get("why") or "")
    for t in reversed(list(getattr(ans, "trace", None) or [])):
        if isinstance(t, str) and t.startswith("support:"):
            rest = t[len("support:"):].strip()
            m = re.match(r"(\w+)\s*(?:[—–-]\s*(.*))?$", rest, re.S)
            if m:
                return m.group(1), m.group(2) or ""
    return None, ""


def basis(why: str) -> str:
    for name, rx in _BASIS:
        if rx.search(why or ""):
            if name == "printed" and (q := _QUOTE.search(why)) and _BARE.fullmatch(q.group(1)):
                return "bare"
            return name
    return "other"


def kind_of(ans) -> str:
    u = str(getattr(ans, "unit", "") or "").strip()
    if u.lower() == "count":
        return "count"
    if _AREA_U.match(u):
        return "area"
    if _LEN_U.match(u):
        return "length"
    for t in getattr(ans, "trace", None) or []:
        if isinstance(t, str) and t.startswith("intent:"):
            w = t.split()
            k = w[1] if len(w) > 1 else ""
            return k if k in KINDS else "other"
    return "other"


def recognised_kind(ans) -> str:
    """The drawing kind stoa.recognise named in the trace ('recognised: {"kind": …}'), or ''."""
    for t in getattr(ans, "trace", None) or []:
        if isinstance(t, str) and t.startswith("recognised:"):
            try:
                return str(json.loads(t[len("recognised:"):]).get("kind") or "")
            except (ValueError, AttributeError):
                return ""
    return ""


def medium_of(ans, path: str | None) -> str:
    kind = recognised_kind(ans)
    suf = Path(str(path or "")).suffix.lower()
    if kind in ("ifc_model", "dxf_plan") or suf in (".ifc", ".dxf", ".dwg"):
        return "structured"
    if "scan" in kind or "photo" in kind:
        return "scan"
    if kind in ("plan_render", "cubicasa_plan") or suf in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp",
                                                           ".heic", ".gif"):
        return "picture"
    return "vector"


def _logit(c: float) -> float:
    c = min(0.98, max(0.02, float(c)))
    return math.log(c / (1 - c))


def features(ans, path: str | None = None) -> dict[str, float]:
    """The calibration's inputs for one answer (also used by experiments/t1/calib_fit.py on eval rows)."""
    state, why = _support_line(ans)
    b = basis(why) if state else "none"
    k = kind_of(ans)
    m = medium_of(ans, path)
    x = {"bias": 1.0, "lc": _logit(getattr(ans, "confidence", 0.0) or 0.0)}
    for s in STATES:
        x[f"s_{s}"] = float(state == s)
    for bb in BASES:
        x[f"b_{bb}"] = float(b == bb)
    for kk in KINDS:
        x[f"k_{kk}"] = float(k == kk)
    for mm in MEDIA[1:]:
        x[f"m_{mm}"] = float(m == mm)
    tr = getattr(ans, "trace", None) or []
    x["agree"] = float(any(isinstance(t, str) and _AGREE.search(t) for t in tr))
    x["missing"] = float(bool(getattr(ans, "missing", None)))
    return x


def _coef() -> dict:
    global _COEF
    if _COEF is None:
        try:
            _COEF = json.loads(COEF_PATH.read_text())
        except (OSError, ValueError):
            _COEF = {}
    return _COEF


def predict(x: dict[str, float], coef: dict | None = None) -> float | None:
    w = (coef if coef is not None else _coef()).get("w")
    if not w:
        return None
    z = sum(float(w.get(k, 0.0)) * v for k, v in x.items())
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def bounded(p: float, state: str | None, why: str = "") -> float:
    b = basis(why)
    if b == "doubt":
        p = min(p, DOUBT_MAX)
    if b == "contra":
        p = min(p, CONTRA_MAX)
    if b == "bare":
        p = min(p, BARE_MAX)
    if b != "printed":
        p = min(p, NOT_SUPPORTED_MAX)  # 'Very sure' = a labelled printed value only (cross-fit: 5 of 7 new >= 0.9
        # wrongs cited a bare number, 1 was 7 count marks)
    if state == "unlocated":
        p = min(p, UNLOCATED_MAX)
    if state != "supported":
        p = min(p, NOT_SUPPORTED_MAX)
    return round(max(0.01, min(0.99, p)), 3)


def calibrate(ans, path: str | None = None):
    """Replace ans.confidence by the fitted P(right) (see the module doc); returns ans (changed in place)."""
    if os.environ.get("STOA_T1_CALIBRATE", "1") == "0":
        return ans
    v = getattr(ans, "value", None)
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return ans  # abstentions, text answers: not what the map was fitted on
    if any(isinstance(t, str) and t.startswith(TRACE_PREFIX) for t in (ans.trace or [])):
        return ans  # once
    if str(getattr(ans, "solver", "") or "").startswith("measure/"):
        return ans  # the visitor's own outline / line: 'Your outline · not checked' (F1 / W5), not a reading of ours
    state, why = _support_line(ans)
    if state is None:
        return ans  # no support check ran (a stub, an early return): outside what the map was fitted on
    keep = next((m for t in ans.trace or [] if isinstance(t, str) for m in [_KEPT_OWN.search(t)] if m), None)
    if keep and recognised_kind(ans) in OWN_KINDS and os.environ.get("STOA_T1_KEEP_OWN", "1") != "0":
        # V1 01:47-01:50 (d32, 23k dataset files uploaded alone): a count the reader did not locate but whose OWN calibrated
        # confidence is >= 0.9 (sanity.uncounted_guard kept it) is right 94-99.5 % of the time (CubiCasa-style apartment
        # plans); the unlocated cap (<= 0.39) took that lane's ECE 0.063 -> 0.241. On Stoa-like sheets such rows: 0. Keep
        # the reader's own value, but never 'Very sure' (nothing is located): <= NOT_SUPPORTED_MAX
        old = float(ans.confidence or 0.0)
        new = round(min(old, NOT_SUPPORTED_MAX), 3)
        ans.confidence = new
        ans.trace = [*(ans.trace or []), f"{TRACE_PREFIX} {old:.2f} -> {new:.2f} (the reader's own calibrated confidence "
                                         "is kept for a count it did not locate: >= 0.9 such counts were 94-99.5 % right on "
                                         "the dataset's apartment plans (V1); never 'Very sure' without marks)"]
        return ans
    if medium_of(ans, path) == "structured":
        return ans  # IFC / DXF readers: deterministic, their own confidences (43 of 1,130 fit rows only)
    x = features(ans, path)
    p = predict(x)
    if p is None:
        return ans
    if any(isinstance(t, str) and _CAP.search(t) for t in ans.trace or []):
        p = min(p, float(ans.confidence or 0.0))  # a guard / the upload solver capped it on purpose: never undo that
    if basis(why) in ("contra", "doubt"):  # T1 c8: a check that speaks AGAINST the answer never raises it (the fit has no
        p = min(p, float(ans.confidence or 0.0))  # weight for c5-c8 reasons: 'contra' lifted sl-269 0.26 -> 0.34)
    new = bounded(p, state, why)
    old = float(ans.confidence or 0.0)
    b = basis(why) if state else "none"
    fam, reader = recognised_kind(ans), str(getattr(ans, "solver", "") or "").split("/")[-1]
    floor = ""
    if (new < old and reader in FAMILY_READERS.get(fam, ()) and os.environ.get("STOA_T1_FAMILY", "1") != "0"):
        new = old  # the map may raise the family's own reader (bim_plan / plan_rooms counts 0.80 -> 0.89, 29 / 29 right), never lower it
        floor = (f"; the reader's own confidence is kept as the floor: a {fam} drawing read by {reader}, the reader "
                 "built for that family (97-99.5 % right at its own confidence on the dataset's uploads, V1)")
    ans.confidence = new
    ans.trace = [*(ans.trace or []), f"{TRACE_PREFIX} {old:.2f} -> {new:.2f} (P(right) for {'an' if (state or 'u')[0] in 'aeiou' else 'a'} {state or 'unchecked'} "
                                     f"{kind_of(ans)} on a {medium_of(ans, path)} drawing, basis '{b}'"
                                     f"{', two methods agree' if x['agree'] else ''}; fitted on d30's upload runs, "
                                     f"experiments/t1/calib_fit.py{floor})"]
    return ans
