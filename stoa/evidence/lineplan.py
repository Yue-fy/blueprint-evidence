"""Room areas and lengths on LINE-ONLY CAD sheets (E1, captain 14:39; X1 wild 14:31).

A sheet whose rooms are drawn as wall lines only (no room fills, no CAD twin, no printed areas) with a printed '1:N'
scale: every reader abstained ('no room polygons') and the prior answered. Here the asked room is found

  1. by its printed LABEL (stacked text lines merged: 'CONFERENCE' / 'ROOM'), the question's words matched to the
     label's words; several equal labels ('MEETING ROOM' x4) need a position word to pick one;
  2. or by its POSITION ('the room at the top right', 'the second room from the top on the left side',
     'das dritte Zimmer von oben auf der linken Seite') among the closed rooms traced on the plan,

then MEASURED with stoa.evidence.linework.measure_room around that point at the printed scale (traced from the wall
lines, door openings closed, line thickness corrected) and answered with the traced outline as Evidence.polygon
(area) or the measured side as Evidence.polyline (length / width), exact=False and an approximate confidence.

    from stoa.evidence.lineplan import answer
    answer(pdf_path, question) -> Answer | None    (None = not this kind of sheet / question; never raises)

Called from plan_rooms when it finds no filled room polygons (stoa/solvers/plan_rooms.py, after ifcplan_pdf).
"""

from __future__ import annotations

import re

from stoa.contract import Answer, Evidence

SOLVER = "lineplan"
_AREA_Q = re.compile(r"\b(area|how big|how large|size|floor space|square (?:met\w*|feet|foot)|quadratmet\w*|qm|m2|m²|sq\.? ?m|fläche|"
                     r"grundfläche|wohnfläche|wie gro(?:ß|ss)|groß|gross|oppervlak\w*|hoe groot|surface|superficie|"
                     r"pinta-ala|kuinka suuri|yta|hur stor|areal)\b", re.I)
_PERIM_Q = re.compile(r"\b(perimeter|circumference|umfang|omtrek|périmètre|perimetre|perimetro|piiri|omkrets)\b", re.I)
_LEN_Q = re.compile(r"\b(how long|length|long side|longer side|longest side|wall to wall|wie lang|länge|lengte|"
                    r"hoe lang|longueur|lunghezza|pituus|kuinka pitkä|längd|hur lång)\b", re.I)
_WID_Q = re.compile(r"\b(how wide|width|short side|shorter side|wie breit|breite|breedte|hoe breed|largeur|"
                    r"larghezza|leveys|kuinka leveä|bredd|hur bred)\b", re.I)
_WALL_Q = re.compile(r"\b(walls?|\w*wand|\w*wände|mur|murs|muri|\w*seinä\w*|\w*vägg\w*|\w*muur)\b", re.I)
_ROOM_Q = re.compile(r"\b(rooms?|\w*raum|\w*räume|\w*zimmer|kamers?|\w*kamer|pièces?|stanz[ae]|huone\w*|offices?|"
                     r"kitchens?|küchen?|bath\w*|bed\w*|living|wohn\w*|schlaf\w*|lobb(?:y|ies)|halls?|foyers?|stores?|"
                     r"storage|garages?|studios?|suites?|units?|apartments?|wohnungen?|flats?|wcs?|toilets?|"
                     r"laundr(?:y|ies)|closets?|corridors?|terraces?|balcon(?:y|ies)|cellars?|keller\w*)\b", re.I)
_NOT_ROOM = re.compile(r"\b(thick\w*|dick\w*|stair\w*|treppe\w*|steps?|"
                       r"door\w*|tür\w*|window\w*|fenster\w*|building|gebäude|site|plot|grundstück|height|high|hoch|"
                       r"höhe|ceiling|decke|apartment|wohnung|flat|unit|storey|floor area of the (?:whole|entire)|gross|footprint|"
                       r"brutto\w*|outside (?:of the )?(?:wall|dimension)\w*|outer (?:dimension|face)s?|"
                       r"(?:area|perimeter|size) of the (?:\w+ )?(?:floor|level|storey)|"
                       r"whole (?:floor|level|plan|house)|entire (?:floor|level|plan|house))\b",
                       re.I)
_COUNT_Q = re.compile(r"\b(how many|number of|count|wie viele|anzahl|hoeveel|combien|quanti|kuinka monta|hur många)\b"
                      r"(?!\s+(?:square|sq\.?|m²|m2|qm|quadrat\w*|neliö\w*|vierkante|mètres? carrés|metri quadrati|"
                      r"kvadrat\w*|feet|ft|metres|meters|metre|meter)\b)", re.I)  # 'how many square metres' = an area
_SUM_Q = re.compile(r"\b(total|all|sum|together|combined|gesamt\w*|alle[rn]?|insgesamt|zusammen|samen|totaal|"
                    r"yhteensä|totalt)\b", re.I)
# words that never name a room on their own
_GENERIC = {"room", "rooms", "raum", "räume", "zimmer", "kamer", "pièce", "stanza", "huone", "rum", "space", "area",
            "the", "this", "that", "what", "which", "how", "big", "large", "long", "wide", "size", "floor", "plan",
            "level", "sheet", "drawing", "its", "side", "longer", "shorter", "wall", "inside", "measured", "along",
            "outer", "with", "and", "from", "der", "die", "das", "des", "dem", "den", "ist", "wie", "groß", "gross",
            "lang", "breit", "von", "auf", "seite", "het", "van", "een", "hoe", "groot", "est", "quelle", "surface",
            "square", "metres", "meters", "metre", "meter", "feet", "one", "there", "here", "where", "located"}
_TITLE_WORDS = re.compile(r"\b(projekt|project|bauherr|client|architekt|architect|ma(?:ß|ss)stab|scale|datum|date|"
                          r"drawn|checked|dateiname|file ?name|sheet|drawing no|rev(?:ision)?|job|title|plan ?nr|"
                          r"blatt|gezeichnet|geprüft|opdrachtgever|schaal|datum)\b", re.I)
_TOP = re.compile(r"\b(top|upper|uppermost|topmost|north(?:ern)?|oben|obere\w*|oberste\w*|boven\w*|haut|en haut|"
                  r"yl[äa]\w*|övre)\b", re.I)
_BOTTOM = re.compile(r"\b(bottom|lower|lowest|south(?:ern)?|unten|untere\w*|unterste\w*|onder\w*|bas|en bas|"
                     r"al[äa]\w*|nedre)\b", re.I)
_LEFT = re.compile(r"\b(left|leftmost|west(?:ern)?|links|linke\w*|linker|gauche|vasen\w*|vänster|sinistra)\b", re.I)
_RIGHT = re.compile(r"\b(right|rightmost|east(?:ern)?|rechts|rechte\w*|rechter|droite|oikea\w*|höger|destra)\b", re.I)
_ORD = [(re.compile(r"\b(first|1st|erste\w*|eerste|premi[eè]re?|ensimmäi\w*)\b", re.I), 1),
        (re.compile(r"\b(second|2nd|zweite\w*|tweede|deuxième|toinen|andra)\b", re.I), 2),
        (re.compile(r"\b(third|3rd|dritte\w*|derde|troisième|kolmas|tredje)\b", re.I), 3),
        (re.compile(r"\b(fourth|4th|vierte\w*|vierde|quatrième|neljäs|fjärde)\b", re.I), 4),
        (re.compile(r"\b(fifth|5th|fünfte\w*|vijfde|cinquième|viides|femte)\b", re.I), 5)]
_FROM_TOP = re.compile(r"\b(from the top|von oben|van boven|depuis le haut|ylhäältä)\b", re.I)
_FROM_BOTTOM = re.compile(r"\b(from the bottom|von unten|van onder|depuis le bas|alhaalta)\b", re.I)
_FROM_LEFT = re.compile(r"\b(from the left|von links|van links|depuis la gauche|vasemmalta)\b", re.I)
_FROM_RIGHT = re.compile(r"\b(from the right|von rechts|van rechts|depuis la droite|oikealta)\b", re.I)


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[^\W\d_]+", s.lower()) if len(w) >= 3]


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    n = min(len(a), len(b))
    return n >= 4 and (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 3


def _compound(lw: str, qw: list[str]) -> set[str]:
    """Question words a run-together label word is made of ('masterbath' -> {'master', 'bathroom'}), else empty."""
    if len(lw) < 7:
        return set()
    for w1 in qw:
        if len(w1) >= 4 and lw.startswith(w1):
            rem = lw[len(w1):]
            for w2 in qw:
                if w2 != w1 and len(rem) >= 3 and (w2.startswith(rem) or _same(w2, rem)):
                    return {w1, w2}
    return set()


def labels(pdf_abs: str, page: int = 0, min_letters: int = 3) -> list[dict]:
    """Room-name labels: text lines with letters, vertically stacked lines of one label merged ('CONFERENCE' over
    'ROOM'). [{"text", "words", "bbox" (pdf_pt), "pt" (centre)}]."""
    from stoa.evidence.linework import text_lines

    lines = []
    for text, b in text_lines(pdf_abs, page):
        if not re.search(r"[^\W\d_]{%d,}" % min_letters, text) or _TITLE_WORDS.search(text) or len(text) > 40:
            continue
        lines.append({"text": text, "bbox": list(b)})
    lines.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
    out: list[dict] = []
    for ln in lines:
        x0, y0, x1, y1 = ln["bbox"]
        h = max(1.0, y1 - y0)
        cx = (x0 + x1) / 2
        hit = None
        for lab in out:
            X0, Y0, X1, Y1 = lab["bbox"]
            if -0.5 * h <= y0 - Y1 <= 0.8 * h and y0 > Y0 and abs(cx - (X0 + X1) / 2) <= 0.25 * max(X1 - X0, x1 - x0) + 2 \
                    and lab["n"] < 3 and abs((Y1 - Y0) / lab["n"] - h) <= 0.35 * h:
                hit = lab
                break
        if hit is None:
            out.append({"text": ln["text"], "bbox": [x0, y0, x1, y1], "n": 1})
        else:
            hit["text"] += " " + ln["text"]
            hit["bbox"] = [min(hit["bbox"][0], x0), hit["bbox"][1], max(hit["bbox"][2], x1), y1]
            hit["n"] += 1
    for lab in out:
        lab["words"] = _words(lab["text"])
        X0, Y0, X1, Y1 = lab["bbox"]
        lab["pt"] = ((X0 + X1) / 2, (Y0 + Y1) / 2)
    return out


def _match_labels(labs: list[dict], question: str) -> list[dict]:
    pos_rx = [_TOP, _BOTTOM, _LEFT, _RIGHT, *(rx for rx, _ in _ORD)]
    qw = [w for w in _words(question) if w not in _GENERIC and not any(rx.fullmatch(w) for rx in pos_rx)]
    if not qw:
        return []
    scored = []
    for lab in labs:
        joined = {w for lw in lab["words"] for w in _compound(lw, qw)}  # 'MASTERBATH' = 'master' + 'bath(room)'
        hit = sum(1 for w in qw if w in joined or any(_same(w, lw) for lw in lab["words"]))
        miss = sum(1 for lw in lab["words"] if lw not in _GENERIC and not any(_same(w, lw) for w in qw)
                   and not _compound(lw, qw))
        if hit:
            scored.append((hit, -miss, lab))
    if not scored:
        return []
    top = max((h, m) for h, m, _ in scored)
    best = [lab for h, m, lab in scored if (h, m) == top]
    # 'bedroom 1' / 'Zimmer 2': the number written after the room word picks 'BEDROOM1' / 'BED ROOM 1' among the ties
    nums = re.findall(r"[^\W\d_]{3,}\s*(\d{1,2})\b(?!\s*(?:m|cm|mm|ft|%|°|\.\d))", question)
    if nums and len(best) > 1:
        byn = [lab for lab in best if any(re.search(rf"(?<!\d)0*{n}(?!\d)", lab["text"]) for n in nums)]
        if byn:
            return byn
    return best


def _position(question: str) -> dict | None:
    q = question.lower()
    v = "top" if _TOP.search(q) else "bottom" if _BOTTOM.search(q) else None
    h = "left" if _LEFT.search(q) else "right" if _RIGHT.search(q) else None
    k = next((n for rx, n in _ORD if rx.search(q)), None)
    order = ("top" if _FROM_TOP.search(q) else "bottom" if _FROM_BOTTOM.search(q) else
             "left" if _FROM_LEFT.search(q) else "right" if _FROM_RIGHT.search(q) else None)
    if v is None and h is None:
        return None
    if order in ("top", "bottom") and v == order:
        v = None
    if order in ("left", "right") and h == order:
        h = None
    return {"v": v, "h": h, "k": k, "order": order}


def _pick(items: list[dict], pos: dict) -> tuple[dict | None, str]:
    """items: [{"pt": (x, y), "bbox": [x0, y0, x1, y1]}] in pdf_pt (y down). The one the position words name."""
    if not items:
        return None, "no rooms"
    X0 = min(i["bbox"][0] for i in items)
    Y0 = min(i["bbox"][1] for i in items)
    X1 = max(i["bbox"][2] for i in items)
    Y1 = max(i["bbox"][3] for i in items)
    W, H = max(1e-6, X1 - X0), max(1e-6, Y1 - Y0)

    def uv(i):
        return (i["pt"][0] - X0) / W, (i["pt"][1] - Y0) / H

    v, h, k, order = pos["v"], pos["h"], pos["k"], pos["order"]
    if order:
        pool = items
        if h == "left":
            pool = [i for i in pool if uv(i)[0] < 0.4]
        elif h == "right":
            pool = [i for i in pool if uv(i)[0] > 0.6]
        if v == "top":
            pool = [i for i in pool if uv(i)[1] < 0.4]
        elif v == "bottom":
            pool = [i for i in pool if uv(i)[1] > 0.6]
        key = {"top": lambda i: uv(i)[1], "bottom": lambda i: -uv(i)[1], "left": lambda i: uv(i)[0],
               "right": lambda i: -uv(i)[0]}[order]
        pool = sorted(pool, key=key)
        n = k or 1
        if len(pool) < n:
            return None, f"only {len(pool)} rooms on that side"
        side = " ".join(x for x in (v, h) if x)
        return pool[n - 1], f"room no. {n} from the {order}" + (f" on the {side} side" if side else "") + \
            f" (of {len(pool)} there)"
    cu = 0.0 if h == "left" else 1.0 if h == "right" else None
    cv = 0.0 if v == "top" else 1.0 if v == "bottom" else None

    def dist(i):
        u, w = uv(i)
        return ((u - cu) ** 2 if cu is not None else 0.0) + ((w - cv) ** 2 if cv is not None else 0.0)

    ranked = sorted(items, key=dist)
    if k and k > 1:
        if len(ranked) < k:
            return None, f"only {len(ranked)} rooms"
        return ranked[k - 1], f"room no. {k} nearest the {' '.join(x for x in (v, h) if x)} of the plan"
    return ranked[0], f"the room nearest the {' '.join(x for x in (v, h) if x)} corner of the plan"


def rooms(pdf_abs: str, page: int, scale_n: float) -> list[dict]:
    """Closed rooms traced on the plan (no seed): free-space components of the linework at a few door-closing gaps,
    merged across readings; regions holding title-block words, running off the page or holding other regions are
    dropped. [{"pt" (most interior point, pdf_pt), "bbox" (pdf_pt), "area_m2" (rough), "readings"}]."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    pg = L._page(pdf_abs, page)
    if pg is None:
        return []
    ppt = pg["ppt"]
    mpp = scale_n * L.PT_TO_M_AT_1 / ppt
    title_px = [((b[0] + b[2]) / 2 * ppt, (b[1] + b[3]) / 2 * ppt) for t, b in L.text_lines(pdf_abs, page)
                if _TITLE_WORDS.search(t)]
    found: list[dict] = []
    for variant in L._variants(pg)[:3]:
        dist = L._dist(pdf_abs, page, variant)
        h, w = dist.shape
        for gap in (0.2, 0.35, 0.5):
            k = max(1, int(round(gap / mpp)))
            free = (dist > k).astype(np.uint8)
            nl, lab, st, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
            for i in range(1, nl):
                x, y, ww, hh, a = st[i]
                if x <= 0 or y <= 0 or x + ww >= w or y + hh >= h:
                    continue
                area = (a + 2 * k * (ww + hh)) * mpp ** 2  # grown back by ~k on every side
                if not 2.0 <= area <= 400:
                    continue
                sub = (lab[y:y + hh, x:x + ww] == i)
                if any(x <= px < x + ww and y <= py < y + hh and sub[int(py) - y, int(px) - x] for px, py in title_px):
                    continue
                dd = cv2.distanceTransform(cv2.copyMakeBorder(sub.astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT,
                                                              value=0), cv2.DIST_L2, 3)[1:-1, 1:-1]
                iy, ix = np.unravel_index(int(np.argmax(dd)), dd.shape)
                seed = ((x + ix) / ppt, (y + iy) / ppt)
                bbox = [(x - k) / ppt, (y - k) / ppt, (x + ww + k) / ppt, (y + hh + k) / ppt]
                same = None
                for f in found:  # one room seen in another reading: its seed falls inside this component
                    fx, fy = f["pt"]
                    if x <= fx * ppt < x + ww and y <= fy * ppt < y + hh and sub[int(fy * ppt) - y, int(fx * ppt) - x]:
                        same = f
                        break
                if same is None:
                    found.append({"pt": seed, "bbox": bbox, "area_m2": round(area, 2), "readings": 1})
                else:
                    same["readings"] += 1
    # a region whose box holds other rooms is the space between a sheet frame and the building (or a table)
    found = [f for f in found if not any(g is not f and f["bbox"][0] <= g["pt"][0] <= f["bbox"][2]
                                         and f["bbox"][1] <= g["pt"][1] <= f["bbox"][3] for g in found)]
    # the plan = the largest group of rooms that touch each other (wall thickness apart); title-block cells and
    # legend boxes form their own small groups
    near = 0.8 / (scale_n * L.PT_TO_M_AT_1)  # 0.8 m on paper, in pt
    groups: list[list[dict]] = []
    for f in found:
        hit = [g for g in groups if any(f["bbox"][0] - near <= h["bbox"][2] and h["bbox"][0] - near <= f["bbox"][2]
                                        and f["bbox"][1] - near <= h["bbox"][3] and h["bbox"][1] - near <= f["bbox"][3]
                                        for h in g)]
        merged = [f] + [h for g in hit for h in g]
        groups = [g for g in groups if g not in hit] + [merged]
    if not groups:
        return []
    return max(groups, key=lambda g: sum(f["area_m2"] for f in g))


def _side_line(poly: list, longest: bool) -> list | None:
    try:
        from shapely.geometry import Polygon

        r = Polygon(poly).minimum_rotated_rectangle
        xy = list(r.exterior.coords)[:4]
        sides = [(xy[i], xy[(i + 1) % 4]) for i in range(2)]
        sides.sort(key=lambda s: (s[0][0] - s[1][0]) ** 2 + (s[0][1] - s[1][1]) ** 2, reverse=longest)
        a, b = sides[0]
        return [[round(a[0], 2), round(a[1], 2)], [round(b[0], 2), round(b[1], 2)]]
    except Exception:  # noqa: BLE001
        return None


_PLURAL_PART = re.compile(r"\b(two|three|four|five|both|all|each|zwei|drei|vier|beide\w*|alle\w*|twee|drie|"
                          r"deux|trois|kaksi|kolme|två|tre|\d+)\b|\w{3,}s\b", re.I)
_SPLIT = re.compile(r"\s*(?:,|\+|\band\b|\bund\b|\ben\b|\bet\b|\bja\b|\boch\b|\bplus\b)\s*", re.I)


def _door_check(pdf: str, page: int, n: float) -> tuple[float | None, int]:
    """Median door-swing radius (m) at 1:n and how many: X1 wild 'exh222' 0.45 m at its printed 1:100 (the view is
    1:200), CTR A101 (Revit) 0.43 m at the key plan's 1:360; LEVEL_11 0.83, German sheet 0.90 (right scales)."""
    from stoa.evidence import linework as L

    radii = L.door_radii_m(pdf, page, n)
    return (radii[len(radii) // 2] if len(radii) >= 3 else None), len(radii)


def _measure_labels(pdf: str, page: int, labs: list[dict], n: float) -> list | None:
    from stoa.evidence import linework as L

    out = []
    for lab in labs:
        m = L.measure_room(pdf, page, lab["pt"], n, tag_box=tuple(lab["bbox"]))
        if m is None:
            return None
        out.append((lab, m))
    return out


def _poly_bbox(poly: list) -> list:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _multi(path, page, n, measured, what, value, conf, how, extra) -> Answer:
    unit = "m2" if what == "area" else "m"
    ev = [Evidence(file=path, page=page, bbox=_poly_bbox(m["polygon"]), bbox_space="pdf_pt", exact=False,
                   polygon=m["polygon"], note=f"{lab['text'][:40]}: {m['area_m2'] if what == 'area' else m['sides_m'][0]} "
                                               f"{'m²' if what == 'area' else 'm'} traced at 1:{n:g} (approximate)")
          for lab, m in measured]
    return Answer(value=value, unit=unit, confidence=conf, evidence=ev, solver=SOLVER,
                  assumptions=[f"scale: 1:{n:g} printed on the sheet; traced from the wall lines (approximate)", how,
                               *extra],
                  missing=["no room area is printed on the sheet: a printed area or a CAD/IFC twin would make it exact"],
                  trace=[f"lineplan: line-only sheet at printed 1:{n:g}; {how}"])


def _equal_rooms_answer(path, pdf, page, n, hits, what) -> Answer | None:
    """Several rooms carry the asked label ('QUIET ROOM' x2): if they measure alike (within 10 %), their mean."""
    if what not in ("area", "length") or len(hits) > 6 or len({h["text"].lower() for h in hits}) != 1:
        return None
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return None
    measured = _measure_labels(pdf, page, hits, n)
    if not measured:
        return None
    vals = [m["area_m2"] if what == "area" else m["sides_m"][0] for _, m in measured if m.get("sides_m")]
    if len(vals) != len(measured) or max(vals) > 1.10 * min(vals):
        return None
    val = round(sum(vals) / len(vals), 2)
    terms = ", ".join(f"{v:g}" for v in vals)
    return _multi(path, page, n, measured, what, val, 0.45,
                  f"{len(hits)} rooms are labelled '{hits[0]['text']}' and measure alike ({terms}): their mean",
                  [f"each outline traced from the drawn wall lines around its label (door openings closed)"])


def _sum_answer(path, pdf, page, q, n, labs, what) -> Answer | None:
    """'the combined floor area of the comms room and the two quiet rooms': every named part matched to its label(s)
    (a plural / number takes every room so labelled), each measured; the sum, with every term's outline."""
    if what != "area":
        return None
    parts = [p for p in _SPLIT.split(q) if p and _words(p)]
    if len(parts) < 2:
        return None
    measured: list = []
    seen: set = set()
    for part in parts:
        hits = _match_labels(labs, part)
        if not hits:
            if any(w not in _GENERIC and not _SUM_Q.fullmatch(w) for w in _words(part)) and part is not parts[0]:
                return None  # a named part with no label on the sheet: the sum would miss it
            continue
        if len(hits) > 1 and not _PLURAL_PART.search(re.sub(r"\b(rooms?|räume|zimmer)\b", "", part, flags=re.I)) \
                and not re.search(r"\b(two|three|four|both|all|zwei|drei|beide\w*|alle\w*)\b", part, re.I):
            return None  # 'the meeting room' with four on the sheet
        for h in hits:
            if id(h) not in seen:
                seen.add(id(h))
                measured.append(h)
    if len(measured) < 2 or len(measured) > 12:
        return None
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return None
    ms = _measure_labels(pdf, page, measured, n)
    if not ms:
        return None
    total = round(sum(m["area_m2"] for _, m in ms), 2)
    terms = " + ".join(f"{m['area_m2']:g} ({lab['text'][:24]})" for lab, m in ms)
    return _multi(path, page, n, ms, what, total, 0.45, f"sum of {len(ms)} rooms found by their labels: {terms} = "
                  f"{total} m²", ["each outline traced from the drawn wall lines around its label (door openings "
                                  "closed)"])


_THICK_Q = re.compile(r"\b(thick\w*|dick\w*|stärke|staerke|wandstärke|dikte|épaisseur|epaisseur|spessore|paksu\w*|"
                      r"tjock\w*|grubo\w*)\b", re.I)
_OUTER_Q = re.compile(r"\b(outer|outside|exterior|external|façade|facade|außen\w*|aussen\w*|buiten\w*|extérieur\w*|"
                      r"esterno|esterni|ulko\w*|yttervägg\w*)\b", re.I)


def _outside(pdf: str, page: int, variant: str):
    """Mask of the free space connected to the page border (the outside of the building), line set `variant`."""
    import cv2
    import numpy as np

    from stoa.evidence import linework as L

    d = L._dist(pdf, page, variant)
    free = (d > 0).astype(np.uint8)
    h, w = free.shape
    ff = np.zeros((h + 2, w + 2), np.uint8)
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if free[seed[1], seed[0]] and not ff[seed[1] + 1, seed[0] + 1]:
            cv2.floodFill(free, ff, seed, 2, flags=4 | (1 << 8) | cv2.FLOODFILL_MASK_ONLY)
    return d, ff[1:-1, 1:-1]


def _wall_samples(pdf, page, n, variant, poly, side, max_m=1.0) -> list:
    """Face-to-face wall thickness (m) behind one side of a traced room outline, sampled across the middle 60 % of
    that side: from the outline (= the inner face line's middle) outward to the last drawn line before the outside."""
    from shapely.geometry import LineString, Polygon

    from stoa.evidence import linework as L

    d, out = _outside(pdf, page, variant)
    ppt = L._page(pdf, page)["ppt"]
    th_px = _stroke_px(L._page(pdf, page))
    mpp = n * L.PT_TO_M_AT_1 / ppt
    P = Polygon(poly)
    x0, y0, x1, y1 = P.bounds
    h, w = d.shape
    res = []
    vert = side in ("top", "bottom")
    lo, hi = (x0, x1) if vert else (y0, y1)
    for i in range(1, 30):
        t = lo + (hi - lo) * (0.2 + 0.6 * i / 30)
        ray = LineString([(t, y0 - 1), (t, y1 + 1)] if vert else [(x0 - 1, t), (x1 + 1, t)])
        cut = P.boundary.intersection(ray)
        pts = [g for g in getattr(cut, "geoms", [cut]) if hasattr(g, "x")]
        if not pts:
            continue
        face = (min if side in ("top", "left") else max)(pts, key=lambda g: g.y if vert else g.x)
        step = -1 if side in ("top", "left") else 1
        fx, fy = int(round(face.x * ppt)), int(round(face.y * ppt))
        runs: list[list[int]] = []  # dark runs along the ray, from a few px INSIDE the room outward
        reached = False
        for k in range(-6, int(max_m / mpp) + 2):
            x, y = (fx, fy + step * k) if vert else (fx + step * k, fy)
            if not (0 <= x < w and 0 <= y < h):
                break
            if out[y, x]:
                reached = True
                break
            if d[y, x] == 0:
                if runs and runs[-1][1] == k - 1:
                    runs[-1][1] = k
                else:
                    runs.append([k, k])
        ab = _runs_thickness(runs, th_px) if reached else None
        if ab is None:
            continue
        a_, b_ = ab  # inner / outer face

        def at(kk):
            return ((fx / ppt, (fy + step * kk) / ppt) if vert else ((fx + step * kk) / ppt, fy / ppt))

        res.append(((b_ - a_) * mpp, at(a_), at(b_)))
    return res


def _runs_thickness(runs: list, th_px: float) -> tuple[float, float] | None:
    """(inner, outer) positions (px along the scan) of a wall's two faces from the dark runs crossed: two or more runs
    = face lines (their middles); ONE solid run = a filled wall (poché), its faces = the run's ends less half a stroke."""
    if not runs:
        return None
    if len(runs) == 1:
        a_, b_ = runs[0][0] + th_px / 2 - 0.5, runs[0][1] - th_px / 2 + 0.5
        return (a_, b_) if b_ - a_ >= 1 else None
    return (runs[0][0] + runs[0][1]) / 2, (runs[-1][0] + runs[-1][1]) / 2


def _stroke_px(pg: dict) -> float:
    ws = sorted(w for _, w, dark in pg["strokes"] if dark) or [0.5]
    return max(1.0, round(ws[len(ws) // 2] * pg["ppt"]))


def _mode(vals: list, tol: float = 0.015) -> tuple[float | None, int]:
    best = (None, 0)
    for v in vals:
        k = sum(1 for u in vals if abs(u - v) <= tol)
        if k > best[1]:
            best = (v, k)
    if best[0] is None:
        return None, 0
    near = sorted(u for u in vals if abs(u - best[0]) <= tol)
    return near[len(near) // 2], best[1]


def _thickness_answer(path: str, pdf: str, page: int, q: str, n: float, labs: list[dict]) -> Answer | None:
    """'How thick is the outer wall at the top of the plan?' / 'the wall between the kitchen and the bath': the
    face-to-face distance (CAD lines stand for the wall faces) measured behind traced room outlines at the printed
    scale; the modal value over many samples (windows / junctions / hatching are outliers)."""
    from shapely.geometry import Point, Polygon

    from stoa.evidence import linework as L

    d_med, _ = _door_check(pdf, page, n)
    if d_med is not None and not 0.6 <= d_med <= 1.3:
        return None
    pg = L._page(pdf, page)
    variant = f"all|doors@{n:g}" if L._door_spokes(pg, n) else "all"
    parts = [p for p in _SPLIT.split(q) if p and _words(p)]
    per = [_match_labels(labs, p) for p in parts] if len(parts) >= 2 else []
    named = [hs[0] for hs in per if len(hs) == 1] if all(len(hs) <= 1 for hs in per) else []
    if len(named) == 2 and named[0] is not named[1] and not _OUTER_Q.search(q):
        ms = _measure_labels(pdf, page, named, n)
        if not ms:
            return None
        A, B = Polygon(ms[0][1]["polygon"]), Polygon(ms[1][1]["polygon"])
        k = n * L.PT_TO_M_AT_1
        d, _ = _outside(pdf, page, variant)
        ppt = pg["ppt"]
        Bpx = B.buffer(-2 / ppt)  # a few px inside B: the scan has crossed B's face line
        found = []
        for t in [i / 240 for i in range(240)]:
            pa = A.exterior.interpolate(t, normalized=True)
            if B.exterior.distance(pa) * k > 0.8:
                continue
            pb = B.exterior.interpolate(B.exterior.project(pa))
            dx, dy = pb.x - pa.x, pb.y - pa.y
            if abs(dx) > 3 * abs(dy):
                ux, uy = (1 if dx > 0 else -1), 0
            elif abs(dy) > 3 * abs(dx):
                ux, uy = 0, (1 if dy > 0 else -1)
            else:
                continue  # diagonal approach (a corner): skip
            x0_, y0_ = pa.x * ppt - 6 * ux, pa.y * ppt - 6 * uy
            runs: list[list[int]] = []
            hit = False
            for j in range(0, int(0.9 / (k / ppt)) + 8):
                x, y = int(round(x0_ + ux * j)), int(round(y0_ + uy * j))
                if not (0 <= y < d.shape[0] and 0 <= x < d.shape[1]):
                    break
                if j > 6 and Bpx.contains(Point(x / ppt, y / ppt)):
                    hit = True
                    break
                if d[y, x] == 0:
                    if runs and runs[-1][1] == j - 1:
                        runs[-1][1] = j
                    else:
                        runs.append([j, j])
            ab = _runs_thickness(runs, _stroke_px(pg)) if hit else None
            if ab is not None:
                a_, b_ = ab
                found.append(((b_ - a_) * k / ppt, [[round((x0_ + ux * a_) / ppt, 2), round((y0_ + uy * a_) / ppt, 2)],
                                                    [round((x0_ + ux * b_) / ppt, 2), round((y0_ + uy * b_) / ppt, 2)]]))
        vals = [v for v, _ in found if 0.03 <= v <= 0.9]
        val, sup = _mode(vals)
        if val is None or sup < 3:
            return None
        line = min(found, key=lambda f_: abs(f_[0] - val))[1]
        how = f"the wall between '{named[0]['text']}' and '{named[1]['text']}'"
        conf_t = 0.35  # a fixture line (bench, counter) along the wall can stand in for the room's face
    else:
        if not _OUTER_Q.search(q):
            return None
        pos = _position(q) or {"v": None, "h": None}
        sides = [x for x in (pos.get("v"), pos.get("h")) if x] or ["top", "bottom", "left", "right"]
        rs = rooms(pdf, page, n)
        if len(rs) < 2:
            return None
        X0, Y0 = min(r["bbox"][0] for r in rs), min(r["bbox"][1] for r in rs)
        X1, Y1 = max(r["bbox"][2] for r in rs), max(r["bbox"][3] for r in rs)
        near_pt = 1.0 / (n * L.PT_TO_M_AT_1)  # rooms whose side lies within 1 m of the plan's extreme on that side
        samples = []
        for side in sides:
            for r in rs:
                b = r["bbox"]
                if {"top": b[1] - Y0, "bottom": Y1 - b[3], "left": b[0] - X0, "right": X1 - b[2]}[side] > near_pt:
                    continue
                m = L.measure_room(pdf, page, r["pt"], n)
                if m:
                    samples += [(*s_, id(r)) for s_ in _wall_samples(pdf, page, n, variant, m["polygon"], side)]
        samples = [s_ for s_ in samples if 0.05 <= s_[0] <= 0.9]
        vals = [s_[0] for s_ in samples]
        val, sup = _mode(vals)
        if val is None or sup < max(4, len(vals) // 3):
            return None
        n_rooms = len({s_[3] for s_ in samples if abs(s_[0] - val) <= 0.015})
        best = min(samples, key=lambda s_: abs(s_[0] - val))
        line = [[round(best[1][0], 2), round(best[1][1], 2)], [round(best[2][0], 2), round(best[2][1], 2)]]
        how = (f"the outer wall{' at the ' + ' '.join(sides) if len(sides) < 4 else 's'}: {sup} of {len(vals)} samples "
               f"behind {n_rooms} room(s) agree")
        conf_t = 0.5 if n_rooms >= 2 else 0.35
    val = round(val, 3)
    x0, y0 = min(line[0][0], line[1][0]), min(line[0][1], line[1][1])
    x1, y1 = max(line[0][0], line[1][0]), max(line[0][1], line[1][1])
    return Answer(value=val, unit="m", confidence=conf_t, solver=SOLVER,
                  evidence=[Evidence(file=path, page=page, bbox=[x0 - 2, y0 - 2, x1 + 2, y1 + 2], bbox_space="pdf_pt",
                                     exact=False, polyline=line,
                                     note=f"wall thickness {val} m (face line to face line) at 1:{n:g} (approximate)")],
                  assumptions=[f"scale: 1:{n:g} printed on the sheet; measured on the drawn wall lines (approximate)",
                               f"{how}; thickness = distance between the two drawn face lines (their middles)"],
                  missing=["plaster / finish layers are not drawn separately: a construction detail would make it exact"],
                  trace=[f"lineplan: wall thickness {val} m — {how}"])


_KINDS = [  # (question words, label pattern) for room kinds counted by their printed labels
    (r"bed\s*rooms?|schlafzimmer|schlafräume|chambres?|slaapkamers?|makuuhuone\w*|sovrum|camere da letto|"
     r"dormitorios?|sypialni\w*",
     r"bed\s*rooms?|bedrm|\bbed\s*\d|master\s*bed|\bbr\s*\d|schlaf\w*|chambre|slaapkamer|makuu\w*|sovrum|"
     r"camera da letto|dormitorio|sypialnia"),
    (r"toilets?|\bw\.?\s?c\.?s?\b|lavator\w*|restrooms?|toiletten?|toilettes?|wc:s|vessa\w*",
     r"\bw\.?\s?c\b\.?|toilet\w*|lavator\w*|restroom|\bvc\b|wc\s*\d"),
    (r"bath\s*rooms?|badezimmer|bäder|salles? de bains?|badkamers?|kylpyhuone\w*|badrum|bagni|baños",
     r"bath\s*room|\bbath\b|en\s*-?\s*suite|\bens\b|\bbad\b|badezimmer|salle de bains?|badkamer|kylpyhuone|"
     r"badrum|bagno|baño"),
    (r"kitchens?|küchen?|cuisines?|keukens?|keittiö\w*|kök|cucin\w*|cocinas?",
     r"kitchen|küche|kueche|cuisine|keuken|keittiö|kök|cucina|cocina"),
]
_OBJ_Q = re.compile(r"\b(windows?|fenster\w*|doors?|tür\w*|tuer\w*|deur\w*|portes?|ovi\w*|ikkun\w*|stairs?|treppe\w*|"
                    r"steps?|stufen|columns?|stütze\w*|säule\w*|pillars?|beams?|walls?|wände|\w*wand|sinks?|"
                    r"washbasins?|bath\s?tubs?|showers?|beds?\b|chairs?|desks?|tables?|parking|spaces? for|cars?|"
                    r"lifts?|elevators?|aufzüge?|radiators?|outlets?|sockets?|lights?)\b", re.I)
_HOW_MANY = re.compile(r"\b(?:how many|number of|count the|wie viele|anzahl (?:der|an)|hoeveel|combien de|"
                       r"quant[ie]|kuinka monta|hur många)\s+(.+?)(?:\s+(?:are|is|does|do|on|in|has|have|there|sind|gibt|"
                       r"hat|zijn|y a|ci sono|on|finns)\b|\?|$)", re.I)


def _count_answer(path: str, pdf: str, page: int, q: str) -> Answer | None:
    """'How many bedrooms / WCs / meeting rooms …?' on a sheet that names its rooms: the printed labels of that kind
    (stacked lines merged, 'BED ROOM 1..3', 'WC'), each boxed (exact text positions). Abstains when no label matches
    (a room drawn without a label is not 'no room')."""
    m = _HOW_MANY.search(q)
    if not m:
        return None
    target = re.sub(r"\([^)]*\)", " ", m.group(1)).strip(" ?.,")
    if re.search(r"\b(bowls?|pans?|seats?|fixtures?|symbols?|basins?|urinals?)\b", m.group(1), re.I):
        return None  # drawn objects ('toilet bowls'), not rooms labelled 'TOILET'
    pat = None
    for qrx, lrx in _KINDS:
        if re.search(qrx, m.group(1), re.I):
            pat = re.compile(lrx, re.I)
            break
    if pat is None:
        # only ROOM phrases ('meeting rooms', 'offices', 'garages'): objects (windows, doors, stairs …) are drawn, and a
        # legend / note label naming them is not one per object (Swiss 'Fenster' legend: 22 windows -> 1, up_lone)
        if _OBJ_Q.search(target) or not _ROOM_Q.search(target):
            return None
        ws = [w for w in re.findall(r"[^\W\d_]+", target.lower()) if w not in _GENERIC or w in ("room", "rooms")]
        ws = [w[:-1] if w.endswith("s") and len(w) > 3 else w for w in ws]
        if not ws or len(ws) > 3 or ws == ["room"] or not any(len(w) >= 4 and w != "room" for w in ws):
            return None  # 'how many rooms' is a room count, not a label kind
        pat = re.compile(r"\b" + r"\s*".join(re.escape(w) + r"s?" for w in ws) + r"\b", re.I)
    if re.search(r"\b(whole|entire|both|all)\s+(house|building|floors|storeys|levels|gebäude|haus)\b|\bin total\b|"
                 r"\binsgesamt\b", q, re.I):
        try:
            import pymupdf

            with pymupdf.open(pdf) as d:
                if len(d) > 1:
                    return None  # one sheet of a SET: the other floors' labels are on other pages (X1 farm x1-147)
        except Exception:  # noqa: BLE001
            return None
    labs = [lab for lab in labels(pdf, page, min_letters=2) if pat.search(lab["text"])]
    if not labs or len(labs) > 60:
        return None
    # a label that STATES a count ('3 BEDROOMS', '2 x WC', '16 RISERS @ 7.5"') is not one object: a text reader must
    # read the number (X1 15:51) -> abstain; room numbers come AFTER the noun ('BED ROOM 2') and still count
    if any(re.search(r"(?<![\d.,/])\d{1,3}\s*(?:[x×]\s*)?$", lab["text"][:mt.start()])
           for lab in labs for mt in [pat.search(lab["text"])]):
        return None
    names = ", ".join(f"'{lab['text'][:24]}'" for lab in labs[:12]) + (" …" if len(labs) > 12 else "")
    return Answer(value=len(labs), unit="count", confidence=0.6, solver=SOLVER,
                  evidence=[Evidence(file=path, page=page, bbox=[round(v, 2) for v in lab["bbox"]], bbox_space="pdf_pt",
                                     exact=True, note=f"{i}: {lab['text'][:40]} (printed label)")
                            for i, lab in enumerate(labs, 1)],
                  assumptions=[f"counted the rooms LABELLED as {target!r} on the sheet: {len(labs)} ({names})"],
                  missing=["a room of that kind drawn without its own label would not be counted"],
                  trace=[f"lineplan: {len(labs)} printed labels match {pat.pattern[:60]!r}: {names}"])


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader of last resort never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    from stoa.data import abspath
    from stoa.evidence import linework as L

    q = question
    if _COUNT_Q.search(q):
        pdf = str(abspath(path))
        return _count_answer(path, pdf, page, q) if pdf.lower().endswith(".pdf") else None
    if _THICK_Q.search(q) and _WALL_Q.search(q) and not _COUNT_Q.search(q):
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        return _thickness_answer(path, pdf, page, q, scales[0], labels(pdf, page)) if scales else None
    if _COUNT_Q.search(q) or _NOT_ROOM.search(q) or (_WALL_Q.search(q) and not _ROOM_Q.search(q)):
        return None  # 'wall to wall' / 'along the outer wall' still ask about a room; 'how long is the wall' does not
    what = ("perimeter" if _PERIM_Q.search(q) else "width" if _WID_Q.search(q) else "length" if _LEN_Q.search(q)
            else "area" if _AREA_Q.search(q) else None)
    if what is None:
        return None
    pdf = str(abspath(path))
    if not pdf.lower().endswith(".pdf"):
        return None
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return None  # no printed scale: a traced outline has no size (W5's 'Set the scale' takes over)
    n = scales[0]
    labs = labels(pdf, page)
    if _SUM_Q.search(q):
        return _sum_answer(path, pdf, page, q, n, labs, what)
    hits = _match_labels(labs, q)
    pos = _position(q)
    how = ""
    tag_box = None
    if hits:
        if len(hits) > 1:
            if pos is None:  # 'one of the quiet rooms': equal rooms measured together; 'the meeting room' x4 differ
                return _equal_rooms_answer(path, pdf, page, n, hits, what)
            pick, why = _pick([{**h, "bbox": h["bbox"]} for h in hits], pos)
            if pick is None:
                return None
            how = f"{len(hits)} labels read '{hits[0]['text']}'; {why}"
        else:
            pick = hits[0]
            how = f"the room labelled '{pick['text']}'"
        seed, tag_box, name = pick["pt"], tuple(pick["bbox"]), pick["text"]
        conf = 0.55
    elif pos is not None:
        rs = rooms(pdf, page, n)
        if len(rs) < 2:
            return None
        pick, why = _pick(rs, pos)
        if pick is None:
            return None
        seed, name = pick["pt"], why
        how = f"{why}, among {len(rs)} closed rooms traced on the plan"
        conf = 0.4
    else:
        return None
    door_med, n_doors = _door_check(pdf, page, n)
    if door_med is not None and not 0.6 <= door_med <= 1.3:
        return None  # door leaves are ~0.7-1.0 m: the printed scale is not this view's (or the arcs are not doors)
    m = L.measure_room(pdf, page, seed, n, tag_box=tag_box)
    if m is None:
        return None
    poly = m["polygon"]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    bbox = [min(xs), min(ys), max(xs), max(ys)]
    if m.get("support", 0) < 3:
        conf = min(conf, 0.35)
    scale_line = f"scale: 1:{n:g} printed on the sheet; traced from the wall lines (approximate)"
    assumptions = [scale_line, f"room = {how}",
                   f"its outline traced from the drawn wall lines around that point (door openings closed at "
                   f"{2 * m['gap_m']:.2f} m, line thickness corrected; {m['support']} of {m['n_readings']} readings "
                   "agree)"]
    missing = ["no room area is printed on the sheet: a printed area or a CAD/IFC twin would make it exact"]
    if door_med is not None:
        assumptions.append(f"scale check: {n_doors} door swings measure a median {door_med:.2f} m at 1:{n:g} "
                           "(a door leaf is ~0.7-1.0 m) ✓")
    sides = m.get("sides_m") or []
    ev_kw = dict(file=path, page=page, bbox=bbox, bbox_space="pdf_pt", exact=False)
    if what == "area":
        val, unit = m["area_m2"], "m2"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: {val} m² traced at 1:{n:g} (approximate)")]
    elif what == "perimeter":
        try:
            from shapely.geometry import Polygon

            val = round(Polygon(poly).length * n * L.PT_TO_M_AT_1, 2)
        except Exception:  # noqa: BLE001
            return None
        unit = "m"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: perimeter {val} m traced at 1:{n:g} (approximate)")]
    else:
        if len(sides) != 2:
            return None
        longest = what == "length"
        val, unit = round(sides[0] if longest else sides[1], 2), "m"
        line = _side_line(poly, longest)
        ev = [Evidence(**ev_kw, polygon=poly, polyline=line,
                       note=f"{name[:50]}: {'long' if longest else 'short'} side {val} m at 1:{n:g} (approximate)")]
        assumptions.append(f"{'length = the longer' if longest else 'width = the shorter'} side of the traced "
                           f"outline's smallest enclosing rectangle ({sides[0]:.2f} x {sides[1]:.2f} m)")
        conf += 0.05
    return Answer(value=val, unit=unit, confidence=round(conf, 3), evidence=ev, solver=SOLVER,
                  assumptions=assumptions, missing=missing,
                  trace=[f"lineplan: line-only sheet at printed 1:{n:g}; {how}; measure_room -> area {m['area_m2']} m², "
                         f"sides {sides}, gap {m['gap_m']} m, variant {m['variant']}, support {m['support']}/"
                         f"{m['n_readings']}"])


def add_traced_outlines(ans: Answer, max_items: int = 6) -> Answer:
    """Captain 14:39: a PRINTED room area (area_tags' answer on a lone vector PDF: 'Zimmer 14.20 m²', 'U116 - 642 SF')
    stays the value; the room's outline traced from the wall lines around its tag is added as evidence when its area
    at the printed scale agrees with the printed one (linework.outline_for_area, +-6 %): an extra approximate item
    (exact=False, the tag's own box stays exact) and a 'cross-check: … ✓' trace line. Never changes value/confidence."""
    try:
        return _add_traced_outlines(ans, max_items)
    except Exception:  # noqa: BLE001
        return ans


def _add_traced_outlines(ans: Answer, max_items: int) -> Answer:
    from stoa.data import abspath
    from stoa.evidence import linework as L

    if ans.value is None or (ans.unit or "").lower() not in ("m2", "m²", "ft2") or \
            (ans.solver or "").split("/")[-1] != "area_tags":
        return ans
    evs = [e for e in ans.evidence if e.bbox and e.bbox_space == "pdf_pt" and e.polygon is None
           and str(e.file).lower().endswith(".pdf")]
    if not evs or len(evs) > max_items or len({(str(e.file), e.page or 0) for e in evs}) != 1:
        return ans
    pdf, page = str(abspath(evs[0].file)), evs[0].page or 0
    scales = L.sheet_scales(pdf, page)
    if not scales:
        return ans
    n = scales[0]
    d, _ = _door_check(pdf, page, n)
    if d is not None and not 0.6 <= d <= 1.3:
        return ans
    added, checks = [], []
    for e in evs:
        m = L._AREA_TAG.search(e.note.split(":", 1)[-1])
        if not m:
            continue
        v = L._num(m.group(1))
        a_m2 = v * 0.09290304 if re.match(r"s|f", m.group(2), re.I) else v
        x0, y0, x1, y1 = e.bbox
        r = L.outline_for_area(pdf, page, ((x0 + x1) / 2, (y0 + y1) / 2), a_m2, n, tag_box=tuple(e.bbox))
        if not r:
            continue
        name = e.note.split(":", 1)[0][:40]
        added.append(Evidence(file=e.file, page=page, bbox=_poly_bbox(r["polygon"]), bbox_space="pdf_pt", exact=False,
                              polygon=r["polygon"], note=f"{name}: outline traced from the wall lines {r['area_m2']} m² "
                                                         f"≈ printed {round(a_m2, 2)} m² ✓ (approximate outline)"))
        checks.append(f"{name} {r['area_m2']} ≈ {round(a_m2, 2)}")
    if added:
        ans.evidence = [*ans.evidence, *added]
        ans.trace = [*ans.trace, f"cross-check: {len(added)} of {len(evs)} printed areas match the room outline traced "
                                 f"from the wall lines at 1:{n:g} (±{L.TOL:.0%}): {'; '.join(checks)} ✓"]
    return ans


__all__ = ["add_traced_outlines", "answer", "labels", "rooms"]
