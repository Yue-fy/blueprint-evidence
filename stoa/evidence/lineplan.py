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


_NEAR_Q = re.compile(r"\b(nearest|closest|next|adjacent|nächst\w*|naechst\w*|neben|dichtstbij\w*|lähin\w*|"
                     r"le plus proche|près)\s+(?:to\s+|zu[mr]?\s+|de\s+la\s+|du\s+)?(?:the\s+|der\s+|dem\s+|de\s+)?"
                     r"([^?.,;]{3,60})", re.I)
_QWORDS = {"what", "which", "how", "is", "are", "the", "of", "in", "on", "this", "total", "floor", "area", "size",
           "net", "gross", "combined", "all", "every", "each", "square", "metres", "meters", "m2", "much", "big"}


def _nearest_to(labs: list[dict], hits: list[dict], ref: str) -> tuple[dict | None, str]:
    """'the Secondary Classroom nearest the north stair' (Ziying's dfe-02): the reference label(s) named after
    'nearest'; a compass / position word picks one of several ('north' = up on the sheet); then the asked room whose
    label lies nearest to it."""
    import math

    refs = _match_labels(labs, ref)
    refs = [r for r in refs if all(r is not h for h in hits)]
    if not refs:
        return None, "no label for the reference"
    note = ""
    if len(refs) > 1:
        pos = _position(ref)
        if pos is None:
            return None, f"{len(refs)} labels read '{refs[0]['text']}'"
        r, _w = _pick(refs, pos)
        if r is None:
            return None, "reference not picked"
        side = " ".join(x for x in (pos["v"], pos["h"]) if x)
        note = (f" (the {side}most of {len(refs)} '{refs[0]['text']}' labels"
                f"{'; north taken as up on the sheet' if re.search(r'north|south|east|west', ref, re.I) else ''})")
        refs = [r]
    rp = refs[0]["pt"]
    ranked = sorted(hits, key=lambda h: math.dist(h["pt"], rp))
    if len(ranked) > 1 and math.dist(ranked[1]["pt"], rp) < 1.15 * math.dist(ranked[0]["pt"], rp):
        return None, "two rooms about as near"
    return ranked[0], f"the one of {len(hits)} nearest the '{refs[0]['text']}' label{note}"


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


def _measure(pdf: str, page: int, pt, n: float, tag_box: tuple):
    """measure_room at the label; when that fails, at the tip of a LEADER ARROW that starts at the label (W5 18:26,
    Stoa's AFCEC A-101: 'Break Room / 2.02 / 153 SF' is printed in the open space below the room with a straight arrow
    pointing into it -> the label's own region is the whole view (3,576 m², holds 'Retail', 'Lobby' ...))."""
    from stoa.evidence import linework as L

    m = L.measure_room(pdf, page, pt, n, tag_box=tag_box)
    if not ARROW_SEED:
        return m
    tip = _arrow_tip(pdf, page, tag_box)
    if tip is None:
        return m
    m2 = L.measure_room(pdf, page, tip, n, tag_box=tag_box, own_radius_pt=0.0)
    if m2 is None:
        return m
    if m is not None:  # the arrow ends in the label's own room (a note / fall arrow): keep it. Containment is not
        try:           # enough: the label's region has its holes filled, so a room inside it 'contains' the tip
            from shapely.geometry import Polygon

            a, b = Polygon(m["polygon"]).buffer(0), Polygon(m2["polygon"]).buffer(0)
            if a.intersection(b).area >= 0.9 * a.union(b).area:
                return m
        except Exception:  # noqa: BLE001
            return m
    return {**m2, "seed_note": f"label outside the room: followed its leader arrow to ({tip[0]:.0f}, {tip[1]:.0f}) pt"}


ARROW_SEED = bool(int(__import__("os").environ.get("E1_ARROW_SEED", "1")))


def _arrow_tip(pdf: str, page: int, box: tuple) -> tuple | None:
    """A straight leader from a label: a one-segment stroke whose tail is within ~2 text heights of the label box and
    whose far end carries a small filled head (arrowhead) -> a point just past the head, inside what it points at."""
    import math

    import numpy as np

    from stoa.evidence import linework as L

    pg = L._page(pdf, page)
    if pg is None or not box:
        return None
    ppt = pg["ppt"]
    th = max(2.0, min(box[2] - box[0], box[3] - box[1]))
    reach = 2.0 * th + 2.0

    def dbox(p):
        dx = max(box[0] - p[0], 0.0, p[0] - box[2])
        dy = max(box[1] - p[1], 0.0, p[1] - box[3])
        return math.hypot(dx, dy)

    heads = []
    for f in pg["fills"]:
        xs, ys = f[:, 0] / ppt, f[:, 1] / ppt
        bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
        tri = abs(float((xs * np.roll(ys, 1) - ys * np.roll(xs, 1)).sum())) / 2 <= 0.65 * bw * bh  # a triangle, not a
        if tri and bw <= 1.5 * th and bh <= 1.5 * th:  # column / wall-end square (EFA 'Science SWR': a wall line + column)
            heads.append((float(xs.mean()), float(ys.mean()), float(max(xs.max() - xs.min(), ys.max() - ys.min()))))
    if not heads:
        return None
    best = None
    lo_x, lo_y, hi_x, hi_y = ((box[0] - reach) * ppt, (box[1] - reach) * ppt, (box[2] + reach) * ppt,
                              (box[3] + reach) * ppt)
    for a, w, dark in pg["strokes"]:
        if not 2 <= len(a) <= 4:
            continue
        if not any(lo_x <= x <= hi_x and lo_y <= y <= hi_y for x, y in a):
            continue  # no end near the label
        pts = [(float(x) / ppt, float(y) / ppt) for x, y in a]
        p, q = max(((u, v) for u in pts for v in pts), key=lambda t: math.hypot(t[0][0] - t[1][0], t[0][1] - t[1][1]))
        L_ = math.hypot(q[0] - p[0], q[1] - p[1])
        if L_ <= 0 or any(abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / L_ > 0.5 for r in pts):
            continue  # one straight line (a path drawn there and back counts), not a polyline
        for s_, e_ in ((p, q), (q, p)):
            ds, de = dbox(s_), dbox(e_)
            ln = math.hypot(e_[0] - s_[0], e_[1] - s_[1])
            if ds > reach or de < ds + 2 * th or ln < 2 * th:
                continue
            h = [hd for hd in heads if math.hypot(hd[0] - e_[0], hd[1] - e_[1]) <= max(1.0 * th, hd[2])]
            if not h:
                continue
            if best is None or ds < best[0]:
                ux, uy = (e_[0] - s_[0]) / ln, (e_[1] - s_[1]) / ln
                k = h[0][2] + 2.0  # past the head
                best = (ds, (e_[0] + ux * k, e_[1] + uy * k))
    return best[1] if best else None


def _measure_labels(pdf: str, page: int, labs: list[dict], n: float) -> list | None:
    from stoa.evidence import linework as L

    out = []
    for lab in labs:
        m = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]))
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
    alt = ""
    if len(parts) == 1 and re.search(r"\b(all|every|each|alle\w*|jede\w*)\b", q, re.I):
        # ONE plural part: 'the total floor area of all Secondary Classrooms' (Stoa DfE sheet, Ziying's dfe-03) = every
        # room whose label holds all the asked words ('Secondary Classroom (Severe …)' included; the exact-label-only
        # total goes into the trace)
        qw = [w for w in _words(_SUM_Q.sub(" ", q)) if w not in _GENERIC and w not in _QWORDS]
        full = [lab for lab in labs if qw and all(any(_same(w, lw) for lw in lab["words"]) for w in qw)]
        exact = _match_labels(labs, q)
        if len(full) < 2 or len(full) > 16:
            return None
        if len(exact) != len(full):
            alt = (f"; only the {len(exact)} labelled exactly '{exact[0]['text']}': see the trace" if exact else "")
        parts, labs_full = [], full
    else:
        labs_full = None
    if len(parts) < 2 and labs_full is None:
        return None
    measured: list = list(labs_full or [])
    seen: set = {id(h) for h in measured}
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
    if labs_full is not None and len(ms) != len(measured):
        return None  # 'all X': a room that could not be traced would be missing from the total
    total = round(sum(m["area_m2"] for _, m in ms), 2)
    terms = " + ".join(f"{m['area_m2']:g} ({lab['text'][:24]})" for lab, m in ms)
    a = _multi(path, page, n, ms, what, total, 0.45, f"sum of {len(ms)} rooms found by their labels: {terms} = "
               f"{total} m²{alt}", ["each outline traced from the drawn wall lines around its label (door openings "
                                    "closed)"])
    if alt:
        ex = {id(x) for x in _match_labels(labs, q)}
        sub = round(sum(m["area_m2"] for lab, m in ms if id(lab) in ex), 2)
        a.trace.append(f"lineplan: alternative = only the rooms labelled exactly '{ms[0][0]['text'] if id(ms[0][0]) in ex else next(lab['text'] for lab, _ in ms if id(lab) in ex)}': {sub} m²")
    return a


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


_BASE_Q = re.compile(r"\b(base ?boards?|skirting(?:[\s-]*boards?)?|skirtings?|base (?:trim|moulding|molding|cove)s?|"
                     r"cove base|wall base|kick ?boards?|sockelleiste\w*|fu(?:ß|ss)leiste\w*|scheuerleiste\w*|"
                     r"plint(?:en|s)?|plinthes?|battiscop\w*|jalkalist\w*|golvlist\w*|sockellist\w*)\b", re.I)
_BASE_WORDS = re.compile(r"\b(linear|lineal|running|metres?|meters?|lm|feet|foot|ft|needs?|needed|required|requires|"
                         r"minus|less|doors?|openings?|perimeter|how|much|many|total|length|of|for|in|does|do|is|are|"
                         r"would|will|we|install|installed|fit|fitted|fitting|along|around|"
                         # ', door opening excluded?' is a condition, not a second room (G1 17:22, sl-091)
                         r"exclud\w*|includ\w*|deduct\w*|subtract\w*|without|not|counted|taken|off|net|gross|"
                         r"ways?|widths?)\b", re.I)
MIN_OPENING_M = 0.5  # a stretch of the room outline with no wall line along it, at least this long, is an opening


def _px(ring, s_pt: float, ppt: float) -> tuple[float, float]:
    q = ring.interpolate(float(s_pt))
    return q.x * ppt, q.y * ppt


def _face_ring(m: dict, m_per_pt: float):
    """The traced outline moved out onto the wall faces: the polygon of pixel centres (simplified) runs ~0.5 pt inside
    the room's pixels, so it is offset by (pixel area - polygon area) / perimeter (X1 sl-055 laundry: 47.9 x 27.7 pt
    -> 48.9 x 28.7 = the wall faces 841.18-890.08 x 938.40-967.14)."""
    from shapely.geometry import LineString, Polygon

    try:
        pg = Polygon([tuple(p) for p in m["polygon"]]).buffer(0)
        if pg.geom_type != "Polygon" or pg.area <= 0:
            return None
        a_px_pt2 = float(m["area_m2"]) / (m_per_pt ** 2)
        d = (a_px_pt2 - pg.area) / pg.length
        if 0 < d <= 2.0:
            g = pg.buffer(d, join_style=2, mitre_limit=2.0)
            if g.geom_type == "Polygon":
                pg = g
        return LineString(list(pg.exterior.coords))
    except Exception:  # noqa: BLE001
        return None


def _skirting(pdf: str, page: int, n: float, m: dict) -> dict | None:
    """Skirting (baseboard) of a traced room = its outline minus the OPENINGS in its walls (captain 16:15, Stoa kind 2):
    an opening is a stretch of the outline with no drawn line along it (door ways closed by the door-swing chord or by
    the gap closing, open passages); the leaf of a door that rests against a wall keeps counting as wall. Opening
    widths are measured jamb to jamb (X1 sl-055: laundry 10.957 m perimeter - 1.02 m opening = 9.93). Returns
    {"perimeter_m", "value_m", "openings": [(a_pt, b_pt, len_m)], "runs": [[pt, ...], ...]} or None."""
    import numpy as np
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    from stoa.evidence import linework as L

    pg = L._page(pdf, page)
    if pg is None or not m.get("polygon"):
        return None
    ppt = pg["ppt"]
    m_per_pt = n * L.PT_TO_M_AT_1
    variant = str(m["variant"])
    d_ink = L.ink_dist(pdf, page)  # the page as printed: walls, jambs, glazing (clip paths respected)
    if d_ink is None:
        return None
    H, W = d_ink.shape

    def at(d, x, y) -> float:
        xi, yi = int(round(x)), int(round(y))
        return float(d[yi, xi]) if 0 <= xi < W and 0 <= yi < H else 255.0

    tol = max(2.5, 0.05 / m_per_pt * ppt)  # px: the outline sits on its lines' middles (simplified polygon: <= 2 px)
    wtol = 0.15 / m_per_pt * ppt  # px: a door leaf resting against a wall lies within this of the wall's line
    leaves, chords = [], []  # door radii: the door way's chord (an opening, drawn or not) / a leaf (counts as wall)
    if "|doors@" in variant:
        spokes, _ = L._door_arcs(pg, n)
        for i in range(0, len(spokes) - 1, 2):
            pair = []
            for sp in spokes[i:i + 2]:
                (cx, cy), (ex, ey) = sp.tolist()
                # drawn = ink along MOST of the radius (median): a door tag ('102') or a leader crossing the door
                # way touches one or two samples (USACE A-101F toilet), a leaf lying on its radius touches all
                mid = float(np.median([at(d_ink, cx + (ex - cx) * t, cy + (ey - cy) * t)
                                       for t in (0.15, 0.25, 0.35, 0.45, 0.5, 0.55, 0.65, 0.75, 0.85)]))
                ext = max(at(d_ink, cx + (ex - cx) * t, cy + (ey - cy) * t) for t in (1.15, 1.3))  # wall line beyond
                pair.append((LineString([(cx, cy), (ex, ey)]), mid, ext))
            if len(pair) != 2:
                continue
            (s0, m0, e0), (s1, m1, e1) = pair
            mtol = min(wtol, 2.5 * ppt)  # a leaf is drawn ON its radius: 0.15 m is 8.5 pt at 1:50 and took in the
            #                              jamb / leaf ends of a 0.9 m door way (A-101F toilet); 1:200 unchanged
            if (m0 > mtol) != (m1 > mtol):  # nothing drawn along one of them: that one spans the door way
                c = 0 if m0 > mtol else 1
            elif (e0 <= tol) != (e1 <= tol):  # both drawn (or both free): the chord runs on into the wall's line
                c = 0 if e0 <= tol else 1
            else:
                leaves.extend(x[0] for x in pair if x[1] <= mtol)
                continue
            chords.append(pair[c][0])
            if pair[1 - c][1] <= mtol:
                leaves.append(pair[1 - c][0])

    def on_chord(x, y) -> bool:
        for c in chords:
            (ax, ay), (bx, by) = c.coords
            vx, vy = bx - ax, by - ay
            ll = vx * vx + vy * vy
            if ll <= 0:
                continue
            t = ((x - ax) * vx + (y - ay) * vy) / ll
            if 0.02 <= t <= 0.98 and abs((x - ax) * vy - (y - ay) * vx) / ll ** 0.5 <= tol:
                return True
        return False

    ring = _face_ring(m, m_per_pt)
    if ring is None:
        return None
    total_pt = ring.length
    step = 1.0 / ppt  # one raster px, in pt
    k = max(8, int(total_pt / step))
    ss = np.linspace(0.0, total_pt, k, endpoint=False)
    d_arr, leaf_arr = np.zeros(k), np.zeros(k, dtype=bool)
    for i, s_ in enumerate(ss):
        x, y = _px(ring, s_, ppt)
        d_arr[i] = at(d_ink, x, y)
        if chords and on_chord(x, y):
            d_arr[i] = 255.0  # a door way: drawn threshold / chord lines do not carry a skirting
        elif d_arr[i] > tol and leaves:
            leaf_arr[i] = any(c.distance(Point(x, y)) <= tol for c in leaves)
    free = (d_arr > tol) & ~leaf_arr
    if free.all() or not free.any():
        return None
    # runs of free samples, the ring rotated so it starts on a supported sample
    r0 = int(np.argmin(free))
    order = np.r_[r0:k, 0:r0]
    opens, cur = [], None
    for idx in order:
        if free[idx]:
            cur = [idx, idx] if cur is None else [cur[0], idx]
        elif cur is not None:
            opens.append(cur)
            cur = None
    if cur is not None:
        opens.append(cur)
    max_walk = int(np.ceil(2 * tol)) + 1
    openings = []
    for i0, i1 in opens:
        # each end walked out to the first sample ON ink (the jamb's line), not along a door leaf
        for _ in range(max_walk):
            j = (i0 - 1) % k
            if free[j] or leaf_arr[j] or d_arr[j] <= 0.5 or j == i1:
                break
            i0 = j
        for _ in range(max_walk):
            j = (i1 + 1) % k
            if free[j] or leaf_arr[j] or d_arr[j] <= 0.5 or j == i0:
                break
            i1 = j
        a_s, b_s = float(ss[i0]) - step / 2, float(ss[i1]) + step / 2
        if b_s < a_s:  # wrapped past the ring's start
            b_s += total_pt
        if openings and a_s - openings[-1][1] <= 2 * tol / ppt:  # a double door: two leaves meet mid-opening
            a_s = openings.pop()[0]
        openings.append((a_s, b_s, (b_s - a_s) * m_per_pt))
    if len(openings) > 1 and openings[0][0] + total_pt - openings[-1][1] <= 2 * tol / ppt:
        last = openings.pop()
        openings[0] = (last[0], openings[0][1] + total_pt, openings[0][1] + total_pt - last[0])
        openings[0] = (openings[0][0], openings[0][1], openings[0][2] * m_per_pt)
    openings = [o for o in openings if o[2] >= MIN_OPENING_M]
    if not openings:
        return {"perimeter_m": round(total_pt * m_per_pt, 3), "value_m": round(total_pt * m_per_pt, 3),
                "openings": [], "runs": [[[round(x, 2), round(y, 2)] for x, y in ring.coords]]}

    def piece(a, b):
        a, b = a % total_pt, b % total_pt
        if a <= b:
            g = substring(ring, a, b)
            return list(g.coords)
        return list(substring(ring, a, total_pt).coords) + list(substring(ring, 0, b).coords)[1:]

    runs, outs = [], []
    for j, (a_s, b_s, ln_m) in enumerate(openings):
        ca = piece(a_s, b_s)
        outs.append(([round(c, 2) for c in ca[0]], [round(c, 2) for c in ca[-1]], round(ln_m, 3)))
        nxt = openings[(j + 1) % len(openings)][0]
        if nxt <= b_s:
            nxt += total_pt
        if nxt - b_s > step:
            runs.append([[round(x, 2), round(y, 2)] for x, y in piece(b_s, nxt)])
    per = total_pt * m_per_pt
    rev = _reveals(ring, total_pt, openings, tol / ppt, m_per_pt) if SKIRT_REVEALS else 0.0
    per -= rev
    val = per - sum(o[2] for o in outs)
    return {"perimeter_m": round(per, 3), "value_m": round(val, 3), "openings": outs, "runs": runs,
            "reveals_m": round(rev, 3)}


SKIRT_REVEALS = bool(int(__import__("os").environ.get("E1_SKIRT_REVEALS", "0")))  # OFF: traced copies cut openings part-way down a jamb (cycle 10)
REVEAL_W = tuple(float(x) for x in __import__("os").environ.get("E1_REVEAL_W", "0.8,1.6").split(","))  # wall-line fit window (m)
REVEAL_MAX_M = 0.7  # a wall's depth: deeper 'recesses' are alcoves / corridors, not door reveals


def _reveals(ring, total_pt: float, openings: list, tol_pt: float, m_per_pt: float) -> float:
    """Door REVEALS on the outline (H 20:37, Ziying's Multi-Purpose Hall on Stoa's DfE PNG: inner wall faces, 'door
    reveals not added'; our trace on the picture ran into every door recess up to the door line, +4.5 m). At each
    opening the room's WALL LINE is fitted through the outline 0.8-1.6 m either side of it (straight, else skipped); the
    outline's EXCURSION beyond that line, on the side away from the room (a recess, <= REVEAL_MAX_M deep), is longer than
    its straight chord by the two jambs -> that excess is not wall face. Independent of where the opening's ends were
    put (on traced copies they sit part-way down a jamb). A column standing into the room bulges the other way."""
    import numpy as np
    from shapely.geometry import Polygon

    def pt(sv):
        q = ring.interpolate(sv % total_pt)
        return np.array([q.x, q.y])

    try:
        cen = np.array(Polygon(ring.coords).centroid.coords[0])
    except Exception:  # noqa: BLE001
        return 0.0
    w0, w1, lim = REVEAL_W[0] / m_per_pt, REVEAL_W[1] / m_per_pt, REVEAL_MAX_M / m_per_pt
    st = max(tol_pt, 0.5)
    out = 0.0
    for a_s, b_s, _ln in openings:
        if b_s - a_s > 3.0 / m_per_pt:
            continue
        back = [pt(a_s - t) for t in np.arange(w0, w1, st)]
        fwd = [pt(b_s + t) for t in np.arange(w0, w1, st)]
        line = None
        # one straight wall line through both sides; else (a door next to a corner) the one straight side alone
        for P in (np.array(back + fwd), np.array(back), np.array(fwd)):
            if len(P) < 4:
                continue
            c = P.mean(axis=0)
            _, _, vt = np.linalg.svd(P - c)
            if float(np.abs((P - c) @ vt[1]).max()) <= 1.5 * tol_pt:
                line = (c, vt[1])
                break
        if line is None:
            continue  # no straight wall line next to the opening
        c, nrm = line

        def dist(q):
            return float((q - c) @ nrm)

        s1 = a_s
        while s1 > a_s - w0 and abs(dist(pt(s1))) > tol_pt:
            s1 -= st
        s2 = b_s
        while s2 < b_s + w0 and abs(dist(pt(s2))) > tol_pt:
            s2 += st
        ds = [dist(pt(x)) for x in np.arange(s1, s2, st)]
        if not ds:
            continue
        far = max(ds, key=abs)
        depth = abs(far)
        if not 0.05 / m_per_pt <= depth <= lim or np.sign(far) == np.sign(dist(cen)):
            continue  # too shallow / too deep, or it bulges INTO the room
        excess = (s2 - s1) - float(np.hypot(*(pt(s2) - pt(s1))))
        if excess > 0:
            out += min(excess, 2 * depth + 2 * tol_pt) * m_per_pt
    return out


_NO_DEDUCT = re.compile(r"\b(?:do(?:es)?\s*n[o’']t|not|without|no)\s+(?:subtract\w*|deduct\w*|minus\w*|remov\w*|"
                        r"tak\w*\s+(?:off|out)|exclud\w*|leav\w*\s+out)\b|\bignor\w*\s+(?:the\s+|any\s+)?(?:doors?|openings?)|"
                        r"\bincluding\s+(?:the\s+|any\s+)?(?:doors?|door\s*ways?|openings?)|\bacross\s+(?:the\s+)?(?:doors?|"
                        r"openings?)|\bgross\b|\bfull\s+perimeter\b", re.I)
_WHOLE_FLOOR_Q = re.compile(r"\b(?:whole|entire|complete|this|the)\s+(?:floor|level|storey|story|plan|building|school|"
                            r"house|apartment|flat|ground floor|first floor)\b|\ball (?:the )?rooms\b|"
                            r"\b(?:every|each) room\b", re.I)
_NO_SKIRTING = re.compile(r"\b(stair\w*|lift\w*|elevator\w*|shaft\w*|riser\w*|void\w*|duct\w*|plant|walkway|"
                          r"canopy|terrace\w*|balcon\w*|courtyard|external|open to below|disclaimer)\b", re.I)


def _iou_poly(a: list, b: list) -> float:
    try:
        from shapely.geometry import Polygon

        pa, pb = Polygon(a).buffer(0), Polygon(b).buffer(0)
        u = pa.union(pb).area
        return pa.intersection(pb).area / u if u > 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _baseboard_answer(path: str, pdf: str, page: int, q: str, n: float, labs: list[dict]) -> Answer | None:
    """'How many linear metres of skirting board does the laundry need?' — every named room (a plural takes every room
    so labelled; 'the laundry and the kitchen' sums) traced, its outline minus the openings in its walls (_skirting).
    Evidence per room: the skirting runs as polylines (outline with the openings cut out) + each opening's width."""
    from stoa.evidence import linework as L

    qn = _BASE_WORDS.sub(" ", _BASE_Q.sub(" ", re.sub(r"\(.*?\)", " ", q)))
    parts = [p for p in _SPLIT.split(qn) if p and _words(p)] or [qn]
    picked: list = []
    for part in parts:
        hits = _match_labels(labs, part)
        if not hits:
            if any(w not in _GENERIC and not _SUM_Q.fullmatch(w) for w in _words(part)) and len(parts) > 1:
                return None  # a named part with no label on the sheet: the total would miss it
            continue
        if len(hits) > 1:
            plural = _PLURAL_PART.search(re.sub(r"\b(rooms?|räume|zimmer)\b", "", part, flags=re.I)) or \
                re.search(r"\b(all|every|each|both|total|alle\w*|jede\w*)\b", q, re.I)
            if not plural:
                pos = _position(q)
                pick, _why = _pick([{**h} for h in hits], pos) if pos else (None, "")
                if pick is None:
                    return None  # 'the group room' with ten on the sheet
                hits = [pick]
        for h in hits:
            if all(h is not p for p in picked):
                picked.append(h)
    whole = False
    rest = [w for w in _words(_WHOLE_FLOOR_Q.sub(" ", qn)) if w not in _GENERIC and not _SUM_Q.fullmatch(w) and
            w not in {"order", "buy", "needed", "need", "required", "metres", "meters", "skirting", "baseboard"}]
    if not picked and not rest and _WHOLE_FLOOR_Q.search(q):  # 'for the whole floor': every room label, each room once
        picked = [lab for lab in labs if not _NO_SKIRTING.search(lab["text"]) and len(lab["text"]) <= 40]
        whole = True
    if not picked or len(picked) > (150 if whole else 16):
        return None
    d_med, n_doors = _door_check(pdf, page, n)
    if d_med is not None and not 0.6 <= d_med <= 1.3:
        return None
    rows, missed = [], []
    for lab in picked:
        m = _measure(pdf, page, lab["pt"], n, tuple(lab["bbox"]))
        sk = _skirting(pdf, page, n, m) if m is not None else None
        if sk is None or (whole and not 1.0 <= m["area_m2"] <= 1500):
            if not whole:
                return None
            missed.append(lab["text"])
            continue
        if whole and any(_iou_poly(m["polygon"], r[1]["polygon"]) > 0.8 for r in rows):
            continue  # two labels in one room ('Changing' over 'and Showers' read apart): counted once
        if not whole:
            try:  # printed dimensions spanning the traced room: its perimeter from the numbers (A-101F toilet:
                #   2 x (1900 + 2054) = 7.908 m minus the 0.91 m door way = 6.998, X1 gold 6.993)
                from stoa.evidence import dimrect as D

                dr = D.rect_spanning(pdf, page, n, tuple(_poly_bbox(m["polygon"])),
                                     tol_pt=0.15 / (n * L.PT_TO_M_AT_1))
            except Exception:  # noqa: BLE001
                dr = None
            if dr is not None:
                per = 2 * (dr["w_mm"] + dr["h_mm"]) / 1000
                sk = {**sk, "perimeter_m": round(per, 3), "value_m": round(per - sum(o[2] for o in sk["openings"]), 3),
                      "dims": (dr["w_mm"], dr["h_mm"])}
        rows.append((lab, m, sk))
    if whole and (len(rows) < 2 or len(missed) > len(rows)):
        return None
    deducted = round(sum(sk["value_m"] for _, _, sk in rows), 2)
    keep_doors = bool(_NO_DEDUCT.search(q))  # 'do not subtract the doors' / 'ignoring door openings' (D1 17:52)
    total = round(sum(sk["perimeter_m"] for _, _, sk in rows), 2) if keep_doors else deducted
    ev, terms = [], []
    for lab, m, sk in rows:
        name = lab["text"][:40]
        ops = sk["openings"]
        op_txt = ", ".join(f"{o[2]:.2f}" for o in ops) or "none"
        dm = sk.get("dims")
        terms.append(f"{name} {sk['perimeter_m']:.2f}" + (f" (printed {dm[0]} x {dm[1]} mm)" if dm else "")
                     + (f" (door reveals {sk['reveals_m']:.2f} m left out)" if sk.get("reveals_m") else "")
                     + f" - ({op_txt}) = {sk['value_m']:.2f} m")
        for i, run in enumerate(sk["runs"]):
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox(run), bbox_space="pdf_pt", exact=False,
                               polyline=run, polygon=m["polygon"] if i == 0 else None,
                               note=f"{name}: skirting {sk['value_m']:.2f} m = outline {sk['perimeter_m']:.2f} m "
                                    f"minus {len(ops)} opening{'s' * (len(ops) != 1)} at 1:{n:g} (approximate)"
                               if i == 0 else f"{name}: skirting run (approximate)"))
        for a, b, ln in ops:
            ev.append(Evidence(file=path, page=page, bbox=_poly_bbox([a, b]), bbox_space="pdf_pt", exact=False,
                               polyline=[a, b], note=f"{name}: opening {ln:.2f} m (no skirting)"))
    conf = 0.5 if all(m.get("support", 0) >= 3 for _, m, _ in rows) else 0.35
    if len(rows) > 1:
        conf = min(conf, 0.45)
    if whole:
        conf = min(conf, 0.3)
    assumptions = [f"scale: 1:{n:g} printed on the sheet; traced from the wall lines (approximate)",
                   ("skirting = the room's whole traced outline (wall faces), door openings NOT deducted as asked; "
                    f"with the openings deducted it would be {deducted:g} m") if keep_doors else
                   "skirting = the room's traced outline (wall faces) minus every opening in its walls, measured jamb "
                   "to jamb (door ways and open passages); built-in units and fixed furniture not deducted",
                   *([f"sum of {len(rows)} rooms: " + "; ".join(terms)] if len(rows) > 1 else [terms[0]]),
                   *([f"whole floor = every labelled room on the sheet (stairs, lifts, shafts, plant and voids left "
                      f"out); {len(missed)} label(s) not traced"
                      + (f": {', '.join(x for x in missed if not re.search(r'[0-9]', x))[:200]}" if missed else "")]
                     if whole else [])]
    if d_med is not None:
        assumptions.append(f"scale check: {n_doors} door swings measure a median {d_med:.2f} m at 1:{n:g} ✓")
    return Answer(value=total, unit="m", confidence=conf, evidence=ev, solver=SOLVER, assumptions=assumptions,
                  missing=["no skirting schedule or room dimensions printed: a finishes schedule or CAD twin would "
                           "make it exact"],
                  trace=[f"lineplan: skirting on a line-only sheet at printed 1:{n:g}: " + "; ".join(terms)])


_RANGE = re.compile(r"(?<![\w.])(\d{1,2})\s*(?:to|through|thru|until|bis|–|—|-)\s*"
                    r"(?:([A-Za-z][\w/&.]*(?:\s+[A-Za-z][\w/&.]*)?)\s+)?(\d{1,2})(?![\w.])", re.I)
_RANGE_STOP = {"of", "the", "from", "between", "in", "and", "for", "all", "area", "areas", "floor", "floors", "level",
               "levels", "storey", "storeys", "story", "stories", "sheet", "sheets", "page", "pages", "grid", "grids",
               "axis", "axes", "bay", "bays", "total", "to", "is", "are", "what", "how", "big", "metres", "meters", "m",
               "mm", "cm", "ft", "sq", "square", "step", "steps", "figure", "fig", "note", "notes", "detail", "details"}


def expand_ranges(q: str) -> str:
    """'English 1 to English 5' / 'Classroom 3-6' -> 'English 1, English 2, English 3, English 4 and English 5' (X1 scan
    sc-077: the numbered range found no label and abstained; the list sums 5 rooms = 277.1 vs gold 275 on the PDF)."""
    def rep(m):
        a, lab2, b = int(m.group(1)), m.group(2), int(m.group(3))
        if not 1 <= b - a <= 11:
            return m.group(0)
        before = q[:m.start()].rstrip().split()
        if lab2:
            k = len(lab2.split())
            lab = " ".join(before[-k:]) if len(before) >= k else ""
            if lab.lower() != lab2.lower():
                return m.group(0)
        else:
            lab = before[-1] if before else ""
        if not lab or not re.fullmatch(r"[A-Za-z][\w/&.]*(?:\s+[A-Za-z][\w/&.]*)?", lab) or \
                lab.split()[-1].lower() in _RANGE_STOP:
            return m.group(0)
        names = [f"{lab} {i}" for i in range(a, b + 1)]
        mid = names[1:-1]
        return f"{a}, {', '.join(mid)} and {names[-1]}" if mid else f"{a} and {names[-1]}"
    return _RANGE.sub(rep, q)


def answer(path: str, question: str, page: int = 0) -> Answer | None:
    try:
        question = expand_ranges(question)
        return _answer(path, question, page)
    except Exception:  # noqa: BLE001 — a reader of last resort never breaks the cascade
        return None


def _answer(path: str, question: str, page: int) -> Answer | None:
    from stoa.data import abspath
    from stoa.evidence import linework as L

    q = question
    if _BASE_Q.search(q):  # 'how many linear metres of skirting …' before the count words
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        return _baseboard_answer(path, pdf, page, q, scales[0], labels(pdf, page)) if scales else None
    if _COUNT_Q.search(q):
        pdf = str(abspath(path))
        return _count_answer(path, pdf, page, q) if pdf.lower().endswith(".pdf") else None
    if _THICK_Q.search(q) and _WALL_Q.search(q) and not _COUNT_Q.search(q):
        pdf = str(abspath(path))
        scales = L.sheet_scales(pdf, page) if pdf.lower().endswith(".pdf") else ()
        return _thickness_answer(path, pdf, page, q, scales[0], labels(pdf, page)) if scales else None
    near_q = _NEAR_Q.search(q)  # 'the classroom nearest the north stair': the stair is a landmark, not the subject
    q_subj = q[:near_q.start()] if near_q else q
    if _COUNT_Q.search(q) or _NOT_ROOM.search(q_subj) or (_WALL_Q.search(q_subj) and not _ROOM_Q.search(q_subj)):
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
    hits = _match_labels(labs, q)
    if _SUM_Q.search(q):
        r = _sum_answer(path, pdf, page, q, n, labs, what)
        if r is not None or len(hits) != 1 or len(_SPLIT.split(q)) > 2:
            return r
        # ONE named room with a 'both / all / total' word ('the multi-purpose hall, both halves together (partition
        # open)', X1 sl-059): that room, measured as one
    near = _NEAR_Q.search(q)
    if near and hits:
        hits = _match_labels(labs, q[:near.start()])
        if not hits:
            return None  # 'the room next to the kitchen': the subject is unnamed, the kitchen only a landmark
    pos = _position(q[:near.start()] if near else q)
    how = ""
    tag_box = None
    why = ""
    if near and len(hits) > 1:
        pick, why = _nearest_to(labs, hits, near.group(2))
        if pick is None:
            return None
        hits, pos = [pick], None
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
            how = f"the room labelled '{pick['text']}'" + (f", {why}" if why else "")
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
    m = _measure(pdf, page, seed, n, tag_box) if tag_box else L.measure_room(pdf, page, seed, n)
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
    if "bridge@" in str(m.get("variant")) and not m.get("reveal_cut"):  # E1 c8 fallback: EFA probe 1 W / 2 L of 3 former abstentions -> rough
        conf = min(conf, 0.4)
        assumptions.append("openings wider than a door were closed along their wall line (the room otherwise runs "
                           "into its neighbours): a rough outline")
    if m.get("seed_note"):
        assumptions.append(m["seed_note"])
    if door_med is not None:
        assumptions.append(f"scale check: {n_doors} door swings measure a median {door_med:.2f} m at 1:{n:g} "
                           "(a door leaf is ~0.7-1.0 m) ✓")
    sides = m.get("sides_m") or []
    ev_kw = dict(file=path, page=page, bbox=bbox, bbox_space="pdf_pt", exact=False)
    dr = None
    if what == "area":
        try:  # printed dimensions spanning exactly this room (each end within 0.15 m): the numbers win (A-101F toilet)
            from stoa.evidence import dimrect as D

            dr = D.rect_spanning(pdf, page, n, tuple(bbox), tol_pt=0.15 / (n * L.PT_TO_M_AT_1))
        except Exception:  # noqa: BLE001
            dr = None
    if dr is not None:
        (hv, *_h), (vv, *_v) = dr["dims"]
        val, unit = dr["area_m2"], "m2"
        ev = [Evidence(**ev_kw, polygon=poly, note=f"{name[:50]}: {hv} x {vv} mm = {val} m² (printed dimensions; "
                                                     f"traced {m['area_m2']} m² at 1:{n:g})"),
              *(Evidence(file=path, page=page, bbox=[round(t, 1) for t in d_[5]], bbox_space="pdf_pt", exact=True,
                         note=f"dimension {d_[0]} mm (printed; measures {d_[6]} mm at 1:{n:g})") for d_ in dr["dims"])]
        assumptions.append(f"area = the printed dimensions {hv} x {vv} mm that span this room (the traced outline "
                           f"gives {m['area_m2']} m²); printed dimensions win over the drawing measured at 1:{n:g}")
        missing = []
        conf = max(conf, 0.6)
    elif what == "area":
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
                         f"{m['n_readings']}",
                         *([f"lineplan: printed dimensions {dr['dims'][0][0]} x {dr['dims'][1][0]} mm span the traced "
                            f"room (each measures its own line: {dr['dims'][0][6]} / {dr['dims'][1][6]} mm) -> "
                            f"{dr['area_m2']} m²"] if dr is not None else [])])


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
