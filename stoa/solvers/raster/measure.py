"""Room areas on raster plans by flood fill from the room's printed label, scaled by the graphic scale bar.

A VLM box around a room is far too rough to measure (Swiss PNG twins, 22 single-room questions: median error 47%,
0 within 2%; experiments/w3/measure_eval.py). Drawn plans have closed rooms: walls are ink, the room is the blank
region around its label. So:
  1. ink = dark pixels (red dash-dot unit outlines and light fills are not walls);
  2. door openings are closed with 1-D closings of ~1 m (from px per m), so the fill stays in the room;
  3. the blank component next to the label (number or name, from the text layer) is the room; its holes (text,
     furniture, fixtures) are filled; a component touching the image border is a leak -> no answer;
  4. area = pixels / (px per m)^2.

    room_area(path, label_box, px_per_m) -> {"m2", "box", "pixels", "notes"} or None
    find_label(layer, question) -> TextItem of the room named in the question ('room 2.32', «Bagno») or None
"""

from __future__ import annotations

import os
import re

import cv2
import numpy as np
from PIL import Image

from stoa.solvers.raster import textlayer as T

_ROOM_ID = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}[a-z]?)(?![\d])")
_QUOTED = re.compile(r"[«\"'‘“„‚]([^«»\"'’”‘“]{2,40})[»\"'’”“]")


def find_label(layer: T.TextLayer, question: str, loose: bool = False) -> T.TextItem | None:
    """The text-layer item that names the room asked about: a room number (exact) first, else a unique quoted name;
    loose=True also takes a unique UNQUOTED label printed on the plan that the question names as a whole word
    ('What is the floor area of the Salón?'), except generic drawing words ('floor' of a 'floor planner' logo)."""
    for rid in _ROOM_ID.findall(question):
        hits = [i for i in layer.items if i.text.strip().rstrip(".") == rid]
        if len(hits) == 1:
            return hits[0]
    for name in _QUOTED.findall(question):
        hits = [i for i in layer.items if T.norm(i.text) == T.norm(name)]
        if len(hits) == 1:
            return hits[0]
    if loose:
        rel = RELATIONAL.search(question)
        q = f" {T.norm(question[:rel.start()] if rel else question)} "  # a room named after 'next to' is a landmark
        qk = re.sub(r"[^\w]+", " ", q)
        hits = [i for i in layer.items if _NAME.fullmatch(T.norm(i.text)) and T.norm(i.text) not in _GENERIC
                and f" {T.norm(i.text)} " in qk]
        if hits and len({T.norm(i.text) for i in hits}) == 1 and len(hits) == 1:
            return hits[0]
        # O1 c6: names joined by '/' ('Food bay/practical resources', 'Head/Mtg' on D1's school sheets) fail _NAME;
        # compare word keys on both sides, and of nested hits ('Hall' inside 'Main Hall') keep the longest
        wide = [i for i in layer.items if (_NAME_WIDE.fullmatch(_wkey(i.text)) or _NAME_NUM.fullmatch(_wkey(i.text))
                                           or LABEL_ABBR and _NAME_LET.fullmatch(_wkey(i.text)))
                and _wkey(i.text) not in _GENERIC and f" {_wkey(i.text)} " in qk]
        keys = {_wkey(i.text) for i in wide}
        top = [k for k in keys if not any(k != o and f" {k} " in f" {o} " for o in keys)]
        if len(top) == 1:
            got = [i for i in wide if _wkey(i.text) == top[0]]
            if len(got) == 1:
                return got[0]
        if hits:
            return None
        if not hits:  # 'living room' on a plan printing WOHNZIMMER / OLOHUONE / SALON
            hits = concept_labels(layer, question[:rel.start()] if rel else question)
            if len(hits) == 1:
                return hits[0]
    return None


# O1 cycle 4: a question in one language, a plan in another ('How big is the living room?' on a 1951 German scan that
# prints WOHNZIMMER; Stoa: Finnish plans, English questions). One concept per room type, the same patterns for the
# question and for the label (the label must BE the name: 'WOHNZIMMER', 'Living room 2', not a sentence). Kitchens,
# bedrooms, bathrooms, saunas, WCs and balconies come from classes.py (Finnish codes included).
CONCEPTS = {
    "living": r"living(?:\s*room)?|lounge|sitting\s*room|family\s*room|wohnzimmer|wohnen|wohnraum|wohnküche|olohuone|"
              r"vardagsrum|s[ée]jour|salon|sal[óo]n|soggiorno|sala\s+de\s+estar|stue|woonkamer|tupa",
    "dining": r"dining(?:\s*room)?|esszimmer|essen|essplatz|ruokailu\w*|ruokahuone|matsal|matplats|salle\s+[àa]\s+manger|"
              r"sala\s+da\s+pranzo|comedor|eetkamer",
    "hall": r"hall(?:way)?|corridor|entrance(?:\s*hall)?|lobby|foyer|flur|diele|eteinen|k[äa]yt[äa]v[äa]|couloir|"
            r"entr[ée]e|corridoio|ingresso|pasillo|recibidor|tambur",
    "storage": r"storage(?:\s*room)?|store\s*room|closet|pantry|abstellraum|abstellkammer|vorrat\w*|speisekammer|"
               r"varasto|vaatehuone|f[öo]rr[åa]d|kl[äa]dkammare|cellier|ripostiglio|dispensa|despensa|trastero",
    "study": r"study|home\s*office|arbeitszimmer|b[üu]ro|ty[öo]huone|kontor|arbetsrum|bureau|studiolo|despacho|estudio",
    "utility": r"utility(?:\s*room)?|laundry|hwr|hauswirtschafts\w*|waschk[üu]che|kodinhoitohuone|khh|tv[äa]ttstuga|"
               r"buanderie|lavanderia|lavadero",
    "garage": r"garage|carport|autotalli|garaje|autorimessa",
    "terrace": r"terrace|patio|terrasse|terassi|terrass|terrazza|terraza",
}
# X1 x1-027 (translated): 'the small room next to the living room' names the living room only as a landmark
RELATIONAL = re.compile(r"\b(next to|beside|adjacent|near(?:est)?|opposite|behind|between|in front of|left of|right of|"
                        r"above|below|across from|off the|neben|gegen[üu]ber|zwischen|hinter|vieress[äa]|vieruss\w*|"
                        r"vastap[äa][äa]t\w*|takana|v[äa]liss[äa]|bredvid|intill|mittemot|[àa] c[ôo]t[ée] d\w*|"
                        r"en face|derri[èe]re|entre|junto a\w*|al lado|frente|detr[áa]s|accanto|vicino|di fronte|"
                        r"dietro|tra|fra)\b", re.I)
_CONCEPT_Q = {k: re.compile(rf"\b(?:{v})\b", re.I) for k, v in CONCEPTS.items()}
_LABEL_CODES = {"living": "oh", "hall": "et|aula", "storage": "vh|var", "dining": "rt|ruokatila"}  # Finnish plan codes
_CONCEPT_L = {k: re.compile(rf"^\s*(?:{v}{'|' + _LABEL_CODES[k] if k in _LABEL_CODES else ''})(?:\s*\d{{0,2}})?\s*[.:]?\s*$",
                            re.I) for k, v in CONCEPTS.items()}


def question_concepts(text: str) -> list[str]:
    """Room concepts a question names, in order of appearance (classes.py room classes included)."""
    from stoa.solvers.raster.classes import ROOM_CLASSES, question_classes

    found = [(m.start(), k) for k, rx in _CONCEPT_Q.items() for m in [rx.search(text)] if m]
    out = [k for _, k in sorted(found)]
    return out + [c for c in question_classes(text) if c in ROOM_CLASSES and c not in out]


def label_concept(text: str) -> str | None:
    from stoa.solvers.raster.classes import ROOM_CLASSES, label_class

    for k, rx in _CONCEPT_L.items():
        if rx.match(text):
            return k
    c = label_class(text)
    return c if c in ROOM_CLASSES else None


def concept_labels(layer: T.TextLayer, question: str, concept: str | None = None) -> list:
    """The labels on the sheet of the room concept the question names (one concept: the first named)."""
    if concept is None:
        cs = question_concepts(question)
        if len(cs) != 1:
            return []
        concept = cs[0]
    return [i for i in layer.items if label_concept(i.text) == concept]


_NAME = re.compile(r"[^\W\d_]{3,}(?: [^\W\d_]{2,}){0,2}")
_NAME_WIDE = re.compile(r"[^\W\d_]{2,}(?: [^\W\d_]{2,}){0,4}")
# O1 c7: numbered names ('English 3', 'Bedroom 2', 'MFL 1', 'Classroom 12a'): one of a series, matched as a whole
_NAME_NUM = re.compile(r"[^\W\d_]{2,}(?: [^\W\d_]{2,}){0,3} \d{1,3}[a-z]?")


def _lkey(text: str) -> str:
    """A label's word key: normalised, every non-word run (/, -, .) a single space ('Head/Mtg' -> 'head mtg')."""
    return re.sub(r"[^\w]+", " ", T.norm(text)).strip()


# O1 c9 (D1's HABS probe): 'Room D' (letter-coded rooms), 'DRAWING RM. 18'-8" × 26'-10".' (a data-table line: name + size,
# 'RM.' = room). STOA_O1_LABEL_ABBR=0 = off.
LABEL_ABBR = os.environ.get("STOA_O1_LABEL_ABBR", "1") != "0"
_NAME_LET = re.compile(r"[^\W\d_]{3,} [^\W\d_]")
_ABBR = {"rm": "room", "rms": "rooms", "bdrm": "bedroom", "bedrm": "bedroom"}
_SIZE_TAIL = re.compile(r"\s+\d[\d'’\"”\s./½¼¾-]*\s*[x×X]\s*\d.*$")


def _wkey(text: str) -> str:
    """_lkey of a label without a printed size after it, abbreviations spelled out ('drawing rm' -> 'drawing room')."""
    if not LABEL_ABBR:
        return _lkey(text)
    k = _lkey(_SIZE_TAIL.sub("", text))
    return " ".join(_ABBR.get(w, w) for w in k.split())

_GENERIC = {"floor", "plan", "plans", "area", "areas", "room", "rooms", "total", "scale", "north", "level", "sheet",
            "drawing", "site", "the", "and", "with", "from", "planner", "page", "date", "title", "project", "unit",
            "apartment", "building", "house", "storey", "story", "wall", "walls", "door", "doors", "window", "windows"}


def _ink(path: str) -> np.ndarray:
    rgb = np.asarray(Image.open(T._abspath(path)).convert("RGB"), dtype=np.int16)
    g = rgb.mean(axis=2)
    red = (rgb[..., 0] > 140) & (rgb[..., 1] < 120) & (rgb[..., 2] < 120)  # unit outlines, annotations
    return ((g < 190) & ~red).astype(np.uint8)


# Chosen on 71 Swiss PNG dev rooms (experiments/w3/room_fill_eval.py --door 0 --hull --clip 3 --split --fixture
# --grow 1), gold = DXF net area: median error 3.8%, within 2% 25/71, 5% 38/71, 10% 47/71, 20% 58/71, pred/gold
# p10/p50/p90 0.77/0.98/1.07. Steps that mattered (each measured): convex hull (fixtures against walls), split open
# regions by nearest room number, walls-only refill when the label sits in a fixture, +1 px border (anti-aliasing:
# 7 -> 25 within 2%). Closing 1-m door gaps up front made it worse (median 29%: it cuts rooms at furniture).
# HELD-OUT check (15 Swiss PNG train drawings never looked at, 30 rooms, 23 measured; artifacts/W3/room_fill_heldout.jsonl):
# within 2% 6/23, 5% 11/23, 10% 15/23, 20% 19/23, median 5.8% (untuned hull: 2/23, median 9.0%). WITHIN = held-out.
# door-swing sectors kept inside a room's hull (0 = off). Swiss PNG bench (room_fill_eval.py --textqa, 242 rooms):
# within 2% 90 -> 91, 5% 135 = 135; LEVEL_11 render comms room 16.32 -> 16.51 m² (gold 16.83)
SEED_SKIP_LABELLED = os.environ.get("STOA_O1_SEED_SKIP", "1") != "0"
# O1 c9 (QA 20:18, W6's DfE phone photo, 31 px/m, no door bridging): the kitchen's blank ran out through a doorway and
# held 109 other room labels (the framed sheet's paper) -> the nearest-label split gave a wedge of exterior, 65.52 m².
# A blank with this many other labels is no room: None (the walls-only refill with door gaps closed is tried next).
LEAK_LABELS = int(os.environ.get("STOA_O1_LEAK_LABELS", "10") or 0)
DOOR_SECTOR_M2 = float(os.environ.get("STOA_O1_SECTOR", "1.2") or 0)
GROW_PER_PX_M = float(os.environ.get("STOA_O1_GROW", "60") or 0)  # border px given back = px_per_m / this (min 1)
OTHER_LABEL_CUT = os.environ.get("STOA_O1_LABEL_CUT", "1") != "0"  # blanks holding another room's label leave the hull
# door gaps bridged along the wall lines (bridge()) on HIGH-RESOLUTION sheets only (>= BRIDGE_MIN_PX_M: large-format CAD
# exported at 300-600 dpi, Stoa's DfE PNG = 118 px/m). D1's line-only school sheets rendered at 600 dpi (O1 c5,
# experiments/o1/c5/lineonly_bench.py): within 2% 25 -> 33 of the first 54 rooms (0 losses), median 5.3 -> 1.0%;
# Stoa's DfE PNG kitchen 40.07 -> 34.97 (alt gold 35.61), laundry 6.79 -> 6.89, fitness 26.46 -> 26.67 (with GROW).
# The Swiss PNG bench (30-59 px/m, residential, open plans) LOSES with it (within 2% 91 -> 60 at 2.0 m): never there.
BRIDGE_M = float(os.environ.get("STOA_O1_BRIDGE", "2.0") or 0)
BRIDGE_MIN_PX_M = float(os.environ.get("STOA_O1_BRIDGE_MIN", "90") or 0)
FILL = {"door_m": 0.0, "hull": True, "clip": True, "clip_m2": 3.0, "fixture_retry": True, "grow": 1,
        "bridge_m": BRIDGE_M}
WITHIN = {"2%": 6 / 23, "5%": 11 / 23, "10%": 15 / 23, "20%": 19 / 23}
CONF = 0.3  # P(within 2%): held-out 6/23 rooms; end-to-end held-out 5/17 at the old 0.35


# NEGATIVE (O1 c6, lineonly_bench.py --dpi 300 --bridge 2 --limit 10, 150 rooms): within 2% hollow 103 -> 93, solid
# 117 -> 114 with it -- the dashes of a grid line in a doorway closed that doorway; kept OFF (STOA_O1_GRID=1 = on)
GRID_DROP = os.environ.get("STOA_O1_GRID", "0") != "0"
GRID_MIN_M = 12.0  # dashed straight lines at least this long are grid / axis lines (they cross rooms), not zone lines
GRID_DENSITY = 0.9  # ink share along a closed long line below this = dashed
DASH_MAX_M = 1.2  # inside a dashed line, ink runs shorter than this are dashes (removed); longer runs are walls (kept)


def drop_grid_lines(ink: np.ndarray, px_per_m: float, min_len_m: float = GRID_MIN_M, gap_m: float = 0.25) -> np.ndarray:
    """Remove DASHED horizontal / vertical lines longer than min_len_m (structural grid lines, captain 18:21 fix list
    item 2): they cross whole buildings, and the fill's 3-px closing turns their dashes into walls that cut rooms. A
    candidate = ink closed along the line over gaps <= gap_m, then opened with a min_len_m line kernel; it is dashed when
    the original ink covers < GRID_DENSITY of it (wall lines are continuous; a door gap is longer than gap_m)."""
    L = max(10, int(round(min_len_m * px_per_m)))
    g = max(3, int(round(gap_m * px_per_m)))
    out = ink.copy()
    for horiz in (True, False):
        kc = (g, 1) if horiz else (1, g)
        ko = (L, 1) if horiz else (1, L)
        closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, kc))
        long_ = cv2.morphologyEx(closed, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, ko))
        n, lab, st, _ = cv2.connectedComponentsWithStats(long_, connectivity=8)
        for c in range(1, n):
            x, y, w, h, a = st[c]
            if (h if horiz else w) > max(4, 0.08 * px_per_m):  # thick: a wall band / poché, never a grid line
                continue
            sub = lab[y:y + h, x:x + w] == c
            dens = float(ink[y:y + h, x:x + w][sub].mean()) if a else 1.0
            if dens >= GRID_DENSITY:
                continue
            # only the DASHES go: ink runs along the line shorter than DASH_MAX_M (a wall drawn on the grid axis is a
            # longer continuous run and stays)
            prof = (ink[y:y + h, x:x + w] & sub).any(axis=0 if horiz else 1)
            kill = np.zeros_like(prof)
            i, n_ = 0, len(prof)
            while i < n_:
                if prof[i]:
                    j = i
                    while j < n_ and prof[j]:
                        j += 1
                    if j - i < DASH_MAX_M * px_per_m:
                        kill[i:j] = True
                    i = j
                else:
                    i += 1
            m = sub & (kill[None, :] if horiz else kill[:, None])
            out[y:y + h, x:x + w][m] = 0
    return out


def bridge(ink: np.ndarray, px_per_m: float, bridge_m: float, run_m: float = 0.4) -> np.ndarray:
    """Close door openings along the WALL LINES only: vertical runs (>= run_m) are closed vertically and horizontal
    runs horizontally, each up to bridge_m. Two collinear wall ends across a doorway join; two parallel walls of a
    narrow room or corridor do not (a plain k x k closing fills every space narrower than k: WCs, stores)."""
    L = max(3, int(round(run_m * px_per_m)))
    k = max(3, int(round(bridge_m * px_per_m)))
    v = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, L)))
    h = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1)))
    v = cv2.morphologyEx(v, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    h = cv2.morphologyEx(h, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    return ink | v | h


def _fill(ink: np.ndarray, label_box, px_per_m: float, door_m: float, hull: bool, clip: bool, clip_m2: float,
          others: list | None, thick: np.ndarray):
    H, W = ink.shape
    k = max(3, int(round(door_m * px_per_m)))
    # always close gaps of >= 3 px (anti-aliasing breaks, dashed outlines); door_m > 0 closes door openings too
    closed = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    closed = cv2.morphologyEx(closed, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    free = (closed == 0).astype(np.uint8)
    n, lab = cv2.connectedComponents(free, connectivity=4)
    # the component around the label: the most common free component in a ring around the label box
    x0, y0, x1, y1 = (int(round(v)) for v in label_box)
    pad = max(4, int(0.15 * px_per_m))
    ring = lab[max(0, y0 - pad):min(H, y1 + pad), max(0, x0 - pad):min(W, x1 + pad)]
    ids, counts = np.unique(ring[ring > 0], return_counts=True)
    if not len(ids):
        return None
    cid = int(ids[np.argmax(counts)])
    if others and len(ids) > 1 and SEED_SKIP_LABELLED:
        # a long label printed across a wall ('Food bay/practical resources' reaching into Yr 6 on D1's school sheet):
        # the ring's biggest blank is the neighbour. Skip blanks that hold ANOTHER label when one without does too.
        held = {int(lab[min(H - 1, int((b[1] + b[3]) / 2)), min(W - 1, int((b[0] + b[2]) / 2))]) for b in others}
        free_ids = [(c, int(i)) for i, c in zip(ids, counts) if int(i) not in held]
        if cid in held and free_ids:
            c, i = max(free_ids)
            if c >= 0.15 * counts.sum():
                cid = i
    comp = (lab == cid).astype(np.uint8)
    ys, xs = np.nonzero(comp)
    if ys.min() == 0 or xs.min() == 0 or ys.max() == H - 1 or xs.max() == W - 1:
        return None  # leaked to the sheet border: not a closed room
    notes = []
    # open-plan rooms merge with a neighbour: other room numbers inside the region -> keep the pixels nearer to ours
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    inside = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in (others or [])
              if comp[min(H - 1, int((b[1] + b[3]) / 2)), min(W - 1, int((b[0] + b[2]) / 2))]
              or lab[min(H - 1, int((b[1] + b[3]) / 2)), min(W - 1, int((b[0] + b[2]) / 2))] == 0
              and xs.min() <= (b[0] + b[2]) / 2 <= xs.max() and ys.min() <= (b[1] + b[3]) / 2 <= ys.max()
              and comp[max(0, int(b[1]) - pad):int(b[3]) + pad, max(0, int(b[0]) - pad):int(b[2]) + pad].any()]
    if LEAK_LABELS and len(inside) >= LEAK_LABELS:
        return None  # the blank holds most of the sheet's rooms: a leak through doorways, not one open-plan room
    if inside:
        d0 = (xs - cx) ** 2 + (ys - cy) ** 2
        keep = np.ones(len(xs), bool)
        for ox, oy in inside:
            keep &= d0 <= (xs - ox) ** 2 + (ys - oy) ** 2
        comp[ys[~keep], xs[~keep]] = 0
        ys, xs = ys[keep], xs[keep]
        notes.append(f"split from {len(inside)} other labelled room(s) in the same open region (nearest label)")
    cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    filled = np.zeros_like(comp)
    if hull:
        cnts = [cv2.convexHull(np.concatenate(cnts))]
    cv2.drawContours(filled, cnts, -1, 1, thickness=cv2.FILLED)
    if inside:  # the hull must not grow back into the neighbours' halves
        filled &= cv2.dilate(comp, np.ones((3, 3), np.uint8), iterations=max(1, int(0.6 * px_per_m)))
    if hull and clip:
        # fixtures and counters against a wall are small separate blanks inside the hull: keep them; take out the
        # thick walls and any OTHER large blank (a neighbouring room the hull of an L-shaped room reaches into)
        sizes = np.bincount(lab.ravel())
        big = sizes > clip_m2 * px_per_m ** 2
        big[0] = False
        big[cid] = False
        if OTHER_LABEL_CUT and others:
            # a small neighbouring room the hull reaches (a 2.6 m² store beside a kitchen, under clip_m2) is still
            # another room when its own label sits in it (Stoa DfE PNG kitchen 36.9 -> store cut out)
            for b in others:
                bx0, by0, bx1, by1 = (int(round(v)) for v in b)
                sub = lab[max(0, by0 - pad):min(H, by1 + pad), max(0, bx0 - pad):min(W, bx1 + pad)]
                oi, oc = np.unique(sub[sub > 0], return_counts=True)
                if len(oi):
                    o = int(oi[np.argmax(oc)])
                    if o != cid:
                        big[o] = True
        cut = big[lab] & (filled > 0)
        if DOOR_SECTOR_M2 > 0 and cut.any():
            # a door swing drawn into the room: the sector between the leaf, the arc and the opening belongs to the
            # corridor's blank (it is open through the doorway), so the clip took it out (LEVEL_11 comms room 16.32
            # vs 16.83 m² = one 0.8 m quarter circle). Small pieces of another blank inside the hull stay.
            nb, bl, bst, _ = cv2.connectedComponentsWithStats(cut.astype(np.uint8), connectivity=4)
            small = np.zeros(nb, bool)
            bw, bh, ba = bst[1:, 2] / px_per_m, bst[1:, 3] / px_per_m, bst[1:, 4].astype(float)
            fillr = ba / np.maximum(1.0, bst[1:, 2] * bst[1:, 3])
            # a quarter circle: about square, side = a door leaf (0.55-1.3 m), covering ~pi/4 of its box
            small[1:] = ((ba <= DOOR_SECTOR_M2 * px_per_m ** 2) & (bw >= 0.55) & (bh >= 0.55) & (bw <= 1.3)
                         & (bh <= 1.3) & (np.maximum(bw, bh) <= 1.6 * np.minimum(bw, bh)) & (fillr >= 0.2)
                         & (fillr <= 0.92))
            cut &= ~small[bl]
        filled[cut | thick] = 0
    # how much of the region's outline runs along thick walls (a room) vs thin lines (a fixture it sits in)
    edge = cv2.morphologyEx(comp, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
    near_wall = cv2.dilate(thick.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    wall_frac = float((edge & near_wall).sum()) / max(1, int(edge.sum()))
    return filled, xs, ys, notes, wall_frac, k


def outline(mask: np.ndarray, eps: float = 1.5) -> list[list[float]] | None:
    """The measured region as a polygon in image px (E1 12:53, D28 Evidence.polygon): the largest outer contour of the
    filled mask, simplified to eps px. None for an empty mask."""
    cs, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cs:
        return None
    c = cv2.approxPolyDP(max(cs, key=cv2.contourArea), eps, True)[:, 0]
    return [[float(x), float(y)] for x, y in c.tolist()] if len(c) >= 3 else None


def wall_structure(path: str, px_per_m: float, dark: int = 70, wall_min_m: float = 0.08,
                   speck_m: float = 0.15) -> np.ndarray:
    """The plan's SOLID walls only: near-black pixels (< dark) that survive an opening of wall_min_m, without specks
    under speck_m. Presentation plans fill walls black and draw furniture in greys with thin black outlines
    (Floorplanner office: walls 0, chairs ~100, desks ~215); the outlines go with the opening. Plans whose walls are
    hatched / outlined only give no closed region here (the caller falls back to the ink fill)."""
    g = np.asarray(Image.open(T._abspath(path)).convert("L"), dtype=np.uint8)
    w = max(2, int(round(wall_min_m * px_per_m)))
    thick = cv2.morphologyEx((g < dark).astype(np.uint8), cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (w, w)))
    n, lab, st, _ = cv2.connectedComponentsWithStats(thick, connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = np.maximum(st[1:, cv2.CC_STAT_WIDTH], st[1:, cv2.CC_STAT_HEIGHT]) >= speck_m * px_per_m
    return keep[lab].astype(np.uint8)


def room_area_walls(path: str, label_box: list[float], px_per_m: float, gap_m: float = 1.8,
                    grow: int = 1) -> dict | None:
    """Room = the region around its label bounded by the SOLID WALLS only, door and window gaps up to gap_m closed.
    For furnished presentation plans (Floorplanner/Sweet Home renders: desks, beds, chairs drawn along every wall)
    where the ink fill stops at the furniture (X1 wild exam: Salón 21.2 m² vs 28.3 measured to the inner wall edges).
    None if the region leaks to the sheet border or the label is not in a free region."""
    walls = wall_structure(path, px_per_m)
    got = _fill(walls, label_box, px_per_m, gap_m, False, False, 3.0, None, walls > 0)
    if got is None:
        return None
    filled, xs, ys, notes, wall_frac, k = got
    if grow > 0:
        filled = cv2.dilate(filled, np.ones((3, 3), np.uint8), iterations=grow)
    px = int(filled.sum())
    m2 = px / px_per_m ** 2
    x0, y0, x1, y1 = label_box
    return {"m2": round(m2, 2), "box": [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)],
            "pixels": px, "wall_frac": round(wall_frac, 2), "polygon": outline(filled), "px_per_m": round(float(px_per_m), 3),
            "notes": [f"walls-only fill from the label at ({(x0 + x1) / 2:.0f}, {(y0 + y1) / 2:.0f}) px (furniture and "
                      f"symbols ignored; door/window gaps up to {gap_m:g} m closed): {px} px / {px_per_m:.2f}² px per m² "
                      f"= {m2:.2f} m²", *notes]}


def room_area(path: str, label_box: list[float], px_per_m: float, door_m: float = 1.05,
              ink: np.ndarray | None = None, wall_m: float = 0.0, hull: bool = False, clip: bool = False,
              clip_m2: float = 3.0, others: list | None = None, fixture_retry: bool = False,
              grow: int = 0, solid: bool = False, bridge_m: float = 0.0, large: bool = False) -> dict | None:
    # large: the caller knows the image is a whole large-format CAD sheet (a printed 1:N on a standard paper size,
    # clean render) -> door gaps are bridged at any resolution (textqa._large; O1 c6: D1's line-only school sheets as
    # 300-dpi PNGs = 59 / 29.5 px/m, below BRIDGE_MIN_PX_M)
    if bridge_m > 0 and (px_per_m >= BRIDGE_MIN_PX_M or large):
        base = _ink(path) if ink is None else ink
        if GRID_DROP:  # large-format CAD: dashed grid lines cross rooms (drop_grid_lines)
            base = drop_grid_lines(base, px_per_m)
        ink = bridge(base, px_per_m, bridge_m)
    r = _room_area(path, label_box, px_per_m, door_m, ink, wall_m, hull, clip, clip_m2, others, fixture_retry, grow)
    if not solid:
        return r
    # furnished plans: the ink fill stops at desks / beds along the walls; the solid-wall region is the room
    try:
        rw = room_area_walls(path, label_box, px_per_m)
    except Exception:  # noqa: BLE001 - an alternative reading, never a reason to fail
        rw = None
    if rw is None or rw["m2"] > SOLID_MAX_M2 or (r is not None and not SOLID_GAIN * r["m2"] <= rw["m2"] <= SOLID_RATIO * r["m2"]):
        return r
    # Swiss PNG check (experiments/o1/room_fill_eval.py --solid, 248 rooms): the only 3 rooms it changed had merged
    # with neighbours through open passages (9.4 -> 78 m²): another room's label inside the region = not one room
    poly = np.asarray(rw.get("polygon") or [], np.float32)
    if len(poly) >= 3 and any(cv2.pointPolygonTest(poly, ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), False) > 0
                              for b in others or []):
        return r
    rw["notes"].append("the ink fill (stopped by furniture / symbols) gave "
                       + (f"{r['m2']:.2f} m²" if r else "no closed region") + "; the solid-wall region is used")
    return rw


SOLID_MAX_M2, SOLID_GAIN, SOLID_RATIO = 120.0, 1.1, 2.0


def room_dims(r: dict, px_per_m: float) -> dict | None:
    """Length x width of a measured room (room_area / room_area_walls result): the minimum-area rectangle around its
    outline. -> {"length_m", "width_m", "fill" (outline area / rectangle area: < ~0.85 = not a rectangle, the
    'dimensions' are its bounding rectangle), "long_side" / "short_side": [[x, y], [x, y]] image px (the measured
    lines, for Evidence.polyline), "rect": 4 corners}."""
    poly = np.asarray(r.get("polygon") or [], np.float32)
    if len(poly) < 3 or not px_per_m:
        return None
    (cx, cy), (w, h), ang = cv2.minAreaRect(poly)
    if w < 2 or h < 2:
        return None
    c = cv2.boxPoints(((cx, cy), (w, h), ang))
    s01, s12 = float(np.hypot(*(c[1] - c[0]))), float(np.hypot(*(c[2] - c[1])))
    a, b = ([c[0], c[1]], [c[1], c[2]]) if s01 >= s12 else ([c[1], c[2]], [c[0], c[1]])
    fill = float(cv2.contourArea(poly)) / max(1.0, w * h)
    return {"length_m": round(max(w, h) / px_per_m, 2), "width_m": round(min(w, h) / px_per_m, 2),
            "fill": round(fill, 3), "long_side": [[float(x), float(y)] for x, y in a],
            "short_side": [[float(x), float(y)] for x, y in b], "rect": [[float(x), float(y)] for x, y in c]}


def _room_area(path, label_box, px_per_m, door_m, ink, wall_m, hull, clip, clip_m2, others, fixture_retry,
               grow) -> dict | None:
    ink = _ink(path) if ink is None else ink
    w = max(2, int(round(0.1 * px_per_m)))
    thick = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (w, w))) > 0
    if wall_m > 0:  # walls only: an opening removes thin lines (furniture, fixtures, door arcs, text strokes)
        ww = max(2, int(round(wall_m * px_per_m)))
        ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (ww, ww)))
    got = _fill(ink, label_box, px_per_m, door_m, hull, clip, clip_m2, others, thick)
    if fixture_retry and (got is None or got[4] < 0.35):
        # the label sits inside a fixture (a bath tub, a shower) or the fill leaked: walls only, door gaps closed
        walls = thick.astype(np.uint8)
        g2 = _fill(walls, label_box, px_per_m, 1.05, hull, clip, clip_m2, others, thick)
        if g2 is not None:
            g2[3].insert(0, "walls-only fill with door gaps closed at 1.05 m (the first fill was bounded by thin "
                            f"lines: {100 * (got[4] if got else 0):.0f}% along walls)")
            got = g2
    if got is None:
        return None
    filled, xs, ys, notes, wall_frac, k = got
    if grow > 0:  # the ink threshold counts the walls' anti-aliased edge as wall: give the room its border back
        if GROW_PER_PX_M > 0:  # high-resolution exports (600 dpi: 118 px/m at 1:200) lose 2-3 px per side
            grow = max(grow, int(round(px_per_m / GROW_PER_PX_M)))
        filled = cv2.dilate(filled, np.ones((3, 3), np.uint8), iterations=grow)
    x0, y0, x1, y1 = label_box
    px = int(filled.sum())
    m2 = px / px_per_m ** 2
    box = [float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)]
    return {"m2": round(m2, 2), "box": box, "pixels": px, "wall_frac": round(wall_frac, 2),
            "polygon": outline(filled), "px_per_m": round(float(px_per_m), 3),
            "notes": [f"flood fill from the label at ({(x0 + x1) / 2:.0f}, {(y0 + y1) / 2:.0f}) px"
                      + (f", door gaps closed at {k} px ({door_m:g} m)" if door_m > 0 else "")
                      + (", convex hull (fixtures against walls kept), walls and other rooms cut out" if hull else "")
                      + f": {px} px / {px_per_m:.2f}² px per m² = {m2:.2f} m²", *notes]}


def room_perimeter(path: str, r: dict, px_per_m: float, ink: np.ndarray | None = None, reach_m: float = 0.3,
                   step: float = 2.0) -> dict | None:
    """Perimeter of a measured room (room_area result) and the door OPENINGS along it: a boundary stretch is an
    opening when a ray cast outward finds no ink within reach_m (a doorway into the corridor; a window keeps its
    glazing lines in the wall, a wall is ink). -> {"perimeter_m", "openings_m", "net_m", "openings": [[x, y], ...]
    (opening sample points), "segments": [[[x, y], [x, y]], ...] (opening runs)}. Baseboard = net_m (captain 16:59)."""
    poly = np.asarray(r.get("polygon") or [], np.float64)
    if len(poly) < 3 or not px_per_m:
        return None
    ink = _ink(path) if ink is None else ink
    H, W = ink.shape
    x0, y0 = int(max(0, poly[:, 0].min() - 4)), int(max(0, poly[:, 1].min() - 4))
    x1, y1 = int(min(W, poly[:, 0].max() + 5)), int(min(H, poly[:, 1].max() + 5))
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(mask, [np.round(poly - [x0, y0]).astype(np.int32)], 1)
    reach = max(3, int(round(reach_m * px_per_m)))
    perim, open_pts, runs, cur = 0.0, [], [], []
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        seg = float(np.hypot(*(b - a)))
        if seg < 1:
            continue
        perim += seg
        t = (b - a) / seg
        nrm = np.array([t[1], -t[0]])
        mid = (a + b) / 2
        probe = np.round(mid + 3 * nrm - [x0, y0]).astype(int)
        if 0 <= probe[1] < mask.shape[0] and 0 <= probe[0] < mask.shape[1] and mask[probe[1], probe[0]]:
            nrm = -nrm  # make the normal point out of the room
        for s in np.arange(0, seg, step):
            p = a + t * s
            hit = False
            for d in range(2, reach + 1):
                q = np.round(p + nrm * d).astype(int)
                if not (0 <= q[0] < W and 0 <= q[1] < H) or ink[q[1], q[0]]:
                    hit = True
                    break
            if not hit:
                open_pts.append([float(p[0]), float(p[1])])
                cur.append([float(p[0]), float(p[1])])
            elif cur:
                runs.append(cur)
                cur = []
        if cur:
            runs.append(cur)
            cur = []
    # an opening narrower than 0.4 m is a gap in the drawing (a dashed line, an anti-aliasing break), not a doorway
    runs = [c for c in runs if len(c) * step >= 0.4 * px_per_m]
    op = sum(len(c) for c in runs) * step / px_per_m
    pm = perim / px_per_m
    return {"perimeter_m": round(pm, 3), "openings_m": round(op, 3), "net_m": round(pm - op, 3),
            "segments": [[c[0], c[-1]] for c in runs], "n_openings": len(runs)}
