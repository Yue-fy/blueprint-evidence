"""Scoring exactly as Stoa describes it.

- count: exact match after rounding the prediction to the nearest int.
- area / length / volume: |pred - gold| <= 2% of |gold|, after converting the predicted unit to the gold unit.
- text / other: not scored for accuracy (None). A rough fuzzy match is reported separately as `text_fuzzy`.
- abstention (value None) or an unparseable value: wrong.
"""

from __future__ import annotations

import math
import re
import unicodedata

REL_TOL = 0.02

# factor to convert 1 <unit> into the base SI unit of its dimension
_UNITS: dict[str, tuple[str, float]] = {}
for names, dim, f in [
    (("m", "meter", "meters", "metre", "metres"), "L", 1.0),
    (("mm", "millimeter", "millimeters", "millimetre", "millimetres"), "L", 1e-3),
    (("cm", "centimeter", "centimeters", "centimetre", "centimetres"), "L", 1e-2),
    (("ft", "feet", "foot", "'"), "L", 0.3048),
    (("in", "inch", "inches", '"'), "L", 0.0254),
    (("m2", "m^2", "m²", "sqm", "sq m", "square meter", "square meters", "square metre", "square metres"), "A", 1.0),
    (("mm2", "mm^2", "mm²"), "A", 1e-6),
    (("cm2", "cm^2", "cm²"), "A", 1e-4),
    (("ft2", "ft^2", "ft²", "sf", "sq ft", "sqft", "square feet", "square foot"), "A", 0.09290304),
    (("m3", "m^3", "m³", "cubic meter", "cubic meters", "cubic metre", "cubic metres"), "V", 1.0),
    (("mm3", "mm^3", "mm³"), "V", 1e-9),
    (("ft3", "ft^3", "ft³", "cf", "cu ft", "cubic feet", "cubic foot"), "V", 0.028316846592),
    (("l", "liter", "liters", "litre", "litres"), "V", 1e-3),
    (("count", "pcs", "pc", "items", "#"), "N", 1.0),
]:
    for n in names:
        _UNITS[n] = (dim, f)


def norm_unit(u: str | None) -> str | None:
    if u is None:
        return None
    u = unicodedata.normalize("NFKC", str(u)).strip().lower().replace("**", "^")
    u = u.replace("²", "2").replace("³", "3")
    u = re.sub(r"\s+", " ", u)
    return u or None


def convert(value: float, from_unit: str | None, to_unit: str | None) -> float | None:
    """Convert value between units of the same dimension. Unknown/None units are assumed equal.
    Returns None when dimensions differ (e.g. m vs m2)."""
    fu, tu = norm_unit(from_unit), norm_unit(to_unit)
    if fu is None or tu is None or fu == tu:
        return value
    a, b = _UNITS.get(fu), _UNITS.get(tu)
    if a is None or b is None:
        return value  # unknown unit string: trust the number
    if a[0] != b[0]:
        return None
    return value * a[1] / b[1]


_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:[ ,  ]\d{3})+(?:\.\d+)?|[-+]?\d+(?:[.,]\d+)?")


# Vulgar fractions: NFKC turns '½' into '1⁄2' (U+2044), so '2½' used to read as 21. Spell them out first.
_VULGAR = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5",
           "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅐": "1/7", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
           "⅑": "1/9", "⅒": "1/10"}
# Feet-inch strings anchored at the first number: 9'-0", 2' 10", 13'-1 1/2", 5'-1/2", 2'-10, 9' (feet only).
_FTIN_RE = re.compile(r"""(?P<ft>\d+(?:\.\d+)?)\s*'(?!')"""
                      r"""(?:\s*-?\s*(?:(?P<inch>\d+(?:\.\d+)?)(?![\d/])(?:(?:\s+|-)(?P<n1>\d+)/(?P<d1>\d+))?"""
                      r"""|(?P<n2>\d+)/(?P<d2>\d+))\s*"?)?""")
# A mixed number '2 1/2' / '2-1/2', or a plain fraction '1/16' (not a date '14/11/2026', not a ratio '1:100').
_MIXED_RE = re.compile(r"(?P<w>\d+)(?:\s+|-)(?P<n>\d+)/(?P<d>\d+)(?![\d/])")
_FRAC_RE = re.compile(r"(?P<n>\d+)\s*/\s*(?P<d>\d+)(?![\d/.,])")


def _prep(v) -> str:
    s = str(v)
    for k, r in _VULGAR.items():
        s = s.replace(k, " " + r)
    s = unicodedata.normalize("NFKC", s).replace("\u2044", "/")
    return s.replace("\u2032", "'").replace("\u2019", "'").replace("\u2033", '"').replace("\u201d", '"').replace("''", '"')


def parse_quantity(v) -> tuple[float | None, str | None]:
    """(number, unit implied by the string's own marks or None). Feet-inch strings ('2\'-10"', '13\'-1 1/2"', '9\'')
    come back as total INCHES with unit 'in'; a fraction or mixed number ('2 1/2', '2½', '1/16') as its value, with
    unit 'in' when an inch mark follows it ('3/4"'); everything else as parse_number always read it, unit None."""
    if v is None or isinstance(v, bool):
        return None, None
    if isinstance(v, (int, float)):
        return (None if (isinstance(v, float) and not math.isfinite(v)) else float(v)), None
    s = _prep(v)
    m0 = _NUM_RE.search(s)
    if not m0:
        return None, None
    p = m0.start()
    sign = -1.0 if s[p] == "-" else 1.0
    if s[p] in "+-":
        p += 1
    if not (p > 0 and s[p - 1] in "/:"):  # '1:100' / '1/100' keep their old reading below
        m = _FTIN_RE.match(s, p)
        if m:
            try:
                inch = float(m.group("inch")) if m.group("inch") else 0.0
                for n, d in (("n1", "d1"), ("n2", "d2")):
                    if m.group(n) and float(m.group(d)) > 0:
                        inch += float(m.group(n)) / float(m.group(d))
                return sign * (float(m.group("ft")) * 12.0 + inch), "in"
            except ValueError:
                pass
        for rx, whole in ((_MIXED_RE, True), (_FRAC_RE, False)):
            m = rx.match(s, p)
            if m and float(m.group("d")) > 0:
                x = (float(m.group("w")) if whole else 0.0) + float(m.group("n")) / float(m.group("d"))
                if not whole and s[m.end():m.end() + 1] == ":":
                    break
                return sign * x, ("in" if s[m.end():].lstrip().startswith('"') else None)
    t = m0.group(0)
    if re.fullmatch(r"[-+]?\d{1,3}(?:[ ,  ]\d{3})+(?:\.\d+)?", t):
        t = re.sub(r"[ ,  ]", "", t)  # thousands separators
    else:
        t = t.replace(",", ".")  # decimal comma
    try:
        return float(t), None
    except ValueError:
        return None, None


def parse_number(v) -> float | None:
    """Best-effort number from a solver value: 12, 12.5, '12,5 m²', '1 234.5', 'about 7', '2 1/2', '2½', '1/16';
    a feet-inch string ('2\'-10"') reads as total inches (see parse_quantity for its unit)."""
    return parse_quantity(v)[0]


def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", str(s)).lower()
    return re.sub(r"[^\w.]+", " ", s).strip()


# Drawing scales as one ratio N (1:N): '1:96', '1/100', '1/8" = 1\'-0"', '1/8' (dataset shorthand),
# '1/8 inch = 1 foot', '1 inch = 12,000 feet'.
_SCALE_IMP = re.compile(r"(\d+(?:\.\d+)?(?:\s*/\s*\d+)?)\s*(?:\"|''|in(?:ch(?:es)?)?\b)\s*=\s*"
                        r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(?:'|ft\b|foot|feet)", re.I)
_SCALE_MET = re.compile(r"(?<![\d.])1\s*:\s*(\d{1,3}(?:[ ,]\d{3})+|\d+(?:[.,]\d+)?)")
# a bare fraction: '1/100' (French '1/100') = 1:100, but the dataset writes imperial scales as '1/4', '3/16'
# (= 1/4" = 1'-0" = 1:48), so a power-of-2 denominator up to 64 means inches per foot
_SCALE_FRAC = re.compile(r"(?<![\d./:])(\d+)\s*/\s*(\d+)(?![\d./])(?!\s*(?:\"|in\b|inch))", re.I)


def scale_ratios(s) -> list[float]:
    """Every drawing scale written in `s`, as N of 1:N, in order of appearance (imperial first if both)."""
    s = unicodedata.normalize("NFKC", str(s or "")).replace("″", '"').replace("′", "'").replace("”", '"')
    out: list[float] = []
    for m in _SCALE_IMP.finditer(s):
        a = m.group(1).replace(" ", "")
        try:
            inch = float(a.split("/")[0]) / float(a.split("/")[1]) if "/" in a else float(a)
        except (ValueError, ZeroDivisionError):
            continue
        if inch > 0:
            out.append(float(m.group(2).replace(",", "")) * 12 / inch)
    rest = _SCALE_IMP.sub(" ", s)
    for m in _SCALE_MET.finditer(rest):
        g = m.group(1)
        v = float(g.replace(" ", "").replace(",", "")) if re.fullmatch(r"\d{1,3}(?:[ ,]\d{3})+", g) else float(g.replace(",", "."))
        if v >= 2:
            out.append(v)
    for m in _SCALE_FRAC.finditer(_SCALE_MET.sub(" ", rest)):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 0 or b <= 1:
            continue
        if b in (2, 4, 8, 16, 32, 64) and a < b:
            out.append(12.0 * b / a)
        elif a == 1:
            out.append(float(b))
    return out


def _same_scale(pred, gold) -> bool | None:
    """None unless both sides state a scale and the gold is short (a scale answer, not a schedule that happens to
    contain 'Panel 1: 68'); else True iff every gold scale is in pred and pred's first scale is in gold."""
    if len(str(gold)) > 40:
        return None
    gs, ps = scale_ratios(gold), scale_ratios(pred)
    if not gs or not ps:
        return None
    near = lambda x, ys: any(abs(x - y) <= REL_TOL * y for y in ys)
    return all(near(g, ps) for g in gs) and near(ps[0], gs)


def text_fuzzy(pred, gold) -> bool:
    """Rough free-text match, reported separately (never part of accuracy).

    Scales compare as ratios first: '1:96' = '1/8" = 1\'-0"', and '1:500' is NOT '1:50' (plain fuzzy matching said it was)."""
    if pred is None or gold is None:
        return False
    sc = _same_scale(pred, gold)
    if sc is not None:
        return sc
    p, g = _norm_text(pred), _norm_text(gold)
    if not p or not g:
        return False
    if p == g or (len(g) >= 2 and f" {g} " in f" {p} "):
        return True
    try:
        from rapidfuzz import fuzz

        # token_SORT, not token_set: token_set gives 100 whenever one side's tokens are a subset ("1" vs
        # "1 water closet, 1 lavatory")
        return fuzz.token_sort_ratio(p, g) >= 85
    except ImportError:  # pragma: no cover
        return False


def score(qtype: str, gold, gold_unit: str | None, pred_value, pred_unit: str | None) -> bool | None:
    """True/False for scored types; None for text/other and for questions without a known answer (not scored)."""
    if qtype in ("text", "other") or gold is None:
        return None
    x, x_mark = parse_quantity(pred_value)
    if x is None:
        return False
    g, g_mark = parse_quantity(gold)
    if g is None:
        return False
    if qtype != "count":
        # a feet-inch string is total inches whatever unit field came with it; a bare '3/4"' only when no unit was given
        if x_mark == "in" and (pred_unit is None or "'" in _prep(pred_value)):
            pred_unit = "in"
        if g_mark == "in" and (gold_unit is None or "'" in _prep(gold)):
            if gold_unit is not None:
                gg = convert(g, "in", gold_unit)
                g = g if gg is None else gg
            else:
                gold_unit = "in"
    if qtype == "count":
        if pred_unit is not None and norm_unit(pred_unit) not in (None, "count") and norm_unit(pred_unit) in _UNITS:
            if _UNITS[norm_unit(pred_unit)][0] != "N":
                return False
        return int(round(x)) == int(round(g))
    y = convert(x, pred_unit, gold_unit)
    if y is None:
        return False
    if g == 0:
        return abs(y) < 1e-9
    return abs(y - g) <= REL_TOL * abs(g) + 1e-12
